"""The extraction queue, the submit door, and the judgement about the text.

This is the **API's** half of ADR 0005. It owns the queue, the lease, the
low-confidence judgement and `datasheet_fts`. It parses nothing: there is no PDF
library imported here or reachable from here, which is the property that keeps the
API image small enough to rebuild in CI on every push. The parsing half is
`app.services.extractors`, imported only by the worker and its tests.

## The queue is a column and an index

`documents.extraction_state` plus `ix_documents_extraction_queue`. No queue table,
because a queue table is a second copy of "which documents exist" that has to be
kept in step with the first one, and the sweep that keeps it in step is the thing
that breaks. A state on the row it describes cannot disagree with itself.

## Claiming is a lease, and the attempt is counted when it is handed out

A worker may be `SIGKILL`ed by a node drain, segfault inside a parser, or lose
power, and in none of those cases does it report anything. So:

* the claim writes `extraction_claimed_at` and the lease **expires on its own**
  after `LEASE_SECONDS`, which is what un-wedges a document whose worker died;
* the claim **increments `extraction_attempts`**, so a document that reliably
  kills whatever picks it up runs out of attempts instead of being re-served
  forever at the head of the queue;
* and after `MAX_EXTRACTION_ATTEMPTS` abandoned leases it is moved to `FAILED`
  rather than left `CLAIMED` with nobody holding it — a state that would be
  invisible to both the pending count and the failure count, which is how a queue
  quietly stops making progress while every dashboard reads clean.

Ordering is `(attempts, id)`: never-tried documents first, so one poison PDF
cannot starve a fresh upload queued behind it.

## Re-extraction is idempotent, keyed on the sha256, with no `client_op_id`

`app.api.idempotency` is deliberately **not** used here, and the reason is not
that the hash is merely sufficient — it is that replay would be *wrong*.

`client_op_id` exists for writes that are not idempotent in themselves: a second
`POST /api/stock/receive` is a second movement, and an append-only ledger cannot
take one back, so the guard replays the first response and writes nothing. Text
submission is the opposite shape. It is keyed on `documents.sha256` (already the
document's identity), it **overwrites** rather than appends, and every field it
writes is a function of the submitted text — so submitting twice lands the same
row and the same index entry by construction, across devices and across restarts,
with nothing stored to recognise the retry by.

And the case that decides it: re-running a document through a *better* extractor
is a legitimate second submission for the same document that **must** overwrite.
A replay guard keyed on the operation would hand back the pypdf answer and
silently discard the Docling one, breaking the exact upgrade path ADR 0005's
Protocol exists to enable. So: no key, no replay, deliberately.

## The flag is derived here, never accepted from the worker

`extracted-chars-per-page ≈ 0` is both the OCR escalation signal and the
low-confidence flag. The submission therefore carries **the page texts**, not a
character count — the API counts them itself. That is not distrust of the worker,
it is removing a state that cannot be reconciled: given a count field, a bug that
reported "3000 chars/page" alongside empty text would store a scanned datasheet as
extracted, confident and empty, and nothing downstream could ever detect it. With
only the text on the wire, the judgement is a function of what is actually stored.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, and_, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.models.documents import Document
from app.models.enums import ExtractionState
from app.models.types import utcnow

#: Media types worth queueing for text extraction. A photograph of a tray has no
#: text layer to read, so it is `NOT_APPLICABLE` from the moment it is stored
#: rather than `PENDING` forever — see `ExtractionState.NOT_APPLICABLE`.
#:
#: Keyed the same way `blobstore.MEDIA_TYPES` is, and deliberately a subset of it:
#: what can be *stored* and what can be *read* are different questions, and an OCR
#: extractor added later would widen this one without touching the store.
EXTRACTABLE_MEDIA_TYPES = frozenset({"application/pdf"})

#: Below this many characters per page, averaged over the document, the text is
#: not trusted: `docs/PLAN.md`'s "extracted-chars-per-page ≈ 0, which is itself
#: the signal to flag low confidence".
#:
#: "≈ 0" is a gulf, not a boundary. A scanned page with no text layer yields 0
#: characters, or a couple of dozen from a stamped page number; a datasheet page
#: with a real text layer yields one to four *thousand*. Any threshold in between
#: separates them, so the value is not delicate — and it is set nearer the low end
#: of the gap on purpose, because the two errors are not symmetric. A false flag
#: costs one unnecessary OCR run. A false pass stores a silently empty document as
#: searched-and-found-nothing, which nobody ever discovers, because the symptom is
#: a search that does not match rather than an error anyone sees.
LOW_CONFIDENCE_CHARS_PER_PAGE = 100.0

#: How long a claim is good for. Generously long: the cost of an over-long lease
#: is a document sitting idle after a worker dies, and the cost of a short one is
#: two workers extracting the same 400-page PDF because the first was merely slow.
#: The first is self-correcting on the next claim; the second wastes the only
#: expensive step in the pipeline.
LEASE_SECONDS = 900

#: How many times one document may be handed out before it is called failed.
#: Three, because the failures worth retrying are transient (a killed pod, a
#: network blip fetching the blob) and a genuinely unparseable PDF fails the same
#: way every time — a fourth attempt buys nothing but another wedged slot.
MAX_EXTRACTION_ATTEMPTS = 3

#: Total extracted characters accepted for one document. A 400-page datasheet at
#: 4k characters a page is 1.6 M, so this is ~5x the largest realistic document.
#: The bound exists because the submission body is JSON held in memory, and an
#: extractor looping on a malformed page tree is how a worker sends a gigabyte.
MAX_EXTRACTED_CHARS = 8_000_000

#: `datasheet_fts` is a **contentful** FTS5 table (no `content=` option — see the
#: migration that created it), so it stores the text as well as indexing it, and
#: `rowid` is `documents.id` exactly as that migration promised. FTS5 supports no
#: upsert, so replacing a document's text is delete-then-insert; doing both in one
#: transaction is what makes a re-extraction atomic from a reader's point of view.
_FTS_DELETE = text("DELETE FROM datasheet_fts WHERE rowid = :document_id")
_FTS_INSERT = text("INSERT INTO datasheet_fts (rowid, text) VALUES (:document_id, :body)")
_FTS_SELECT = text("SELECT text FROM datasheet_fts WHERE rowid = :document_id")

#: Page texts are joined with a newline for the index. The character count is the
#: **sum of the page lengths**, never `len(joined)`: the separators are this
#: module's formatting, and counting them would make the judgement depend on how
#: many pages a document happens to have.
#:
#: Blank pages are dropped from the join rather than contributing a bare separator,
#: which is what makes a page-image PDF come out as the **empty string** instead of
#: `"\n\n\n"`. That matters at the wire: a client distinguishes "read, found nothing"
#: from "not read yet" as `""` versus `null`, and whitespace is truthy in every
#: language one of them will be written in. Nothing is lost — the joined text carries
#: no page offsets anyway, so a separator between two blanks separates nothing.
_PAGE_SEPARATOR = "\n"


class ExtractionError(ValueError):
    """A submission or requeue that cannot be recorded as asked.

    Carries a `reason` the route maps to a status, matching `blobstore.BlobError`
    and `labels.LabelError`, so a route has one class to catch and one vocabulary
    to map.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# The judgement
# ---------------------------------------------------------------------------


def initial_state(media_type: str) -> ExtractionState:
    """The extraction state a freshly stored document starts in.

    Called from `app.services.documents.store_document`, which is the only place a
    `documents` row is created, so this is the one place the decision is made.
    """
    declared = media_type.split(";", 1)[0].strip().lower()
    if declared in EXTRACTABLE_MEDIA_TYPES:
        return ExtractionState.PENDING
    return ExtractionState.NOT_APPLICABLE


def chars_per_page(char_count: int | None, page_count: int | None) -> float | None:
    """The escalation signal, computed in exactly one place.

    None when there is no judgement to make (nothing extracted yet). A document
    reporting zero pages gets `0.0` rather than a `ZeroDivisionError`: a PDF whose
    page tree yielded nothing is precisely the sort that needs flagging, so it must
    reach the threshold test rather than blow up before it.
    """
    if char_count is None or page_count is None:
        return None
    if page_count <= 0:
        return 0.0
    return char_count / page_count


def is_low_confidence(char_count: int, page_count: int) -> bool:
    """Whether extracted text is too sparse to trust — the stored judgement.

    Averaged over the document rather than judged per page, because the escalation
    it triggers (re-read this with OCR) is per document too. Per-page granularity
    would flag every datasheet with a full-page schematic or a blank verso, which
    is most of them.
    """
    ratio = chars_per_page(char_count, page_count)
    return ratio is None or ratio < LOW_CONFIDENCE_CHARS_PER_PAGE


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentText:
    """What is known about one document's text, extracted or not.

    Every field is None-able and the None case is **normal**: a document whose
    text has not been read is the ADR's first-class state, so this dataclass has
    to be able to say "stored, served, attached, no text" without that reading as
    a failure. `state` is what a caller should branch on.
    """

    state: ExtractionState
    #: None when nothing has been extracted. Empty string when a page-image PDF
    #: was extracted and genuinely yielded nothing — a distinction that matters,
    #: because the second one has been looked at and flagged.
    body: str | None
    page_count: int | None
    char_count: int | None
    #: See `document_text.chars_per_page`.
    chars_per_page: float | None
    #: None until a judgement exists. Never `False` by default — see the column.
    low_confidence: bool | None
    extractor: str | None
    extracted_at: datetime | None
    attempts: int
    error: str | None


def text_of(session: Session, document: Document) -> str | None:
    """The extracted text, straight out of the index that stores it.

    Read from `datasheet_fts` rather than from a column, because that table is
    contentful and duplicating the text into `documents` would mean two copies to
    disagree. Phase 6's `enrichment.extract.ExtractionRequest.document_text` is
    the other caller this exists for.
    """
    row = session.execute(_FTS_SELECT, {"document_id": document.id}).scalar_one_or_none()
    return None if row is None else str(row)


def describe(session: Session, document: Document) -> DocumentText:
    """Everything a client needs to render "no text yet" without calling it broken."""
    return DocumentText(
        state=ExtractionState(document.extraction_state),
        body=text_of(session, document),
        page_count=document.page_count,
        char_count=document.text_char_count,
        chars_per_page=chars_per_page(document.text_char_count, document.page_count),
        low_confidence=document.text_low_confidence,
        extractor=document.extractor,
        extracted_at=document.extracted_at,
        attempts=document.extraction_attempts,
        error=document.extraction_error,
    )


def status_counts(session: Session) -> dict[ExtractionState, int]:
    """Queue depth by state, every state present even at zero.

    Zeros included deliberately: a dashboard that shows a key only when it is
    non-zero cannot distinguish "nothing failed" from "the failure count stopped
    being reported", and `docs/PLAN.md` wants failed extractions as a health check.
    """
    rows = session.execute(
        select(Document.extraction_state, func.count()).group_by(Document.extraction_state)
    ).all()
    counted = {ExtractionState(state): count for state, count in rows}
    return {state: counted.get(state, 0) for state in ExtractionState}


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def _claimable(cutoff: datetime) -> ColumnElement[bool]:
    """The predicate for "a worker may take this now".

    Built once and used by **both** the select that picks candidates and the
    update that takes them, so the two cannot drift apart — and so the update is a
    genuine compare-and-swap: a document another worker claimed in between no
    longer satisfies this, so the update passes over it instead of stealing it.
    """
    return and_(
        Document.extraction_attempts < MAX_EXTRACTION_ATTEMPTS,
        or_(
            Document.extraction_state == ExtractionState.PENDING,
            # An expired lease. `extraction_claimed_at` is stored as fixed-width
            # ISO-8601 text (see `app.models.types.UtcDateTime`), which is exactly
            # why this comparison is sound in SQL: lexicographic order is
            # chronological order.
            and_(
                Document.extraction_state == ExtractionState.CLAIMED,
                Document.extraction_claimed_at < cutoff,
            ),
        ),
    )


def expire_abandoned(
    session: Session, *, now: datetime | None = None, lease_seconds: int = LEASE_SECONDS
) -> int:
    """Fail every claim whose lease ran out with no attempts left. Returns the count.

    Run at the top of every `claim`, so it needs no scheduler to be correct — the
    queue repairs itself as a side effect of being used, which is the only sweep
    that can be relied on to have run.

    Without this a thrice-killed worker leaves a document sitting in `CLAIMED`
    with nobody holding it: not pending, not failed, not claimable, and absent from
    every count that would have shown the queue had stopped moving.
    """
    moment = now or utcnow()
    cutoff = moment - timedelta(seconds=lease_seconds)
    # `Session.execute` is typed as returning `Result`, which does not declare
    # `rowcount`; a DML statement always yields a `CursorResult`, which does. Same
    # cast, same reason, as `app.db.maintenance.rebuild_lot_balances`.
    result = cast(
        "CursorResult[Any]",
        session.execute(
            update(Document)
            .where(
                Document.extraction_state == ExtractionState.CLAIMED,
                Document.extraction_claimed_at < cutoff,
                Document.extraction_attempts >= MAX_EXTRACTION_ATTEMPTS,
            )
            .values(
                extraction_state=ExtractionState.FAILED,
                extraction_claimed_at=None,
                extraction_error=(
                    f"abandoned: {MAX_EXTRACTION_ATTEMPTS} leases expired with no result submitted"
                ),
            )
        ),
    )
    session.flush()
    return result.rowcount


def _candidates(session: Session, *, cutoff: datetime, limit: int) -> list[int]:
    """The ids `claim` will try to take. A named function, not an inlined query.

    Two reasons. It keeps the "pick" and the "take" visibly separate, which is what
    makes the compare-and-swap in `claim` legible as one. And it is the seam a test
    substitutes to hand `claim` a **stale** candidate list — the interleave a real
    concurrent claim produces, which nothing in a single-threaded test can reach
    otherwise, leaving the re-check untested and therefore free to be deleted by
    somebody tidying up.
    """
    return list(
        session.execute(
            select(Document.id)
            .where(_claimable(cutoff))
            # Fresh work before retries; `id` makes the order total so the batch a
            # given queue state produces is deterministic and therefore testable.
            .order_by(Document.extraction_attempts, Document.id)
            .limit(limit)
        ).scalars()
    )


def claim(
    session: Session,
    *,
    worker_id: str,
    limit: int = 1,
    now: datetime | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> list[Document]:
    """Hand out up to `limit` documents needing text, taking a lease on each.

    `now` and `lease_seconds` are parameters rather than reads of the clock so the
    dead-worker path is testable without sleeping for fifteen minutes; the route
    passes neither.

    **Pick, then take, and the take re-checks.** Route handlers are `def`, so FastAPI
    runs them in a threadpool: two concurrent `POST /api/extraction/claims` are two
    threads on two connections, and pysqlite does not hold a read transaction open
    across the `SELECT` below — so another claim genuinely can land between the pick
    and the take. The `update` therefore repeats `_claimable`, which makes it a
    compare-and-swap: the loser's statement matches nothing and it gets a shorter
    batch. It is never resolved by both workers getting the same document, which
    would double the only expensive step in the pipeline.
    """
    moment = now or utcnow()
    cutoff = moment - timedelta(seconds=lease_seconds)

    expire_abandoned(session, now=moment, lease_seconds=lease_seconds)

    candidates = _candidates(session, cutoff=cutoff, limit=limit)
    if not candidates:
        return []

    # `RETURNING` is what makes the batch exactly what this statement flipped, so
    # the ids come out of the compare-and-swap itself rather than being inferred
    # afterwards. The previous re-read matched on `(claimed_by, claimed_at,
    # CLAIMED)`, which is **not unique per call**: two claims by one worker in the
    # same microsecond each reported everything that worker held, so `limit=1`
    # answered with N documents and the worker re-extracted work already in flight
    # — the exact double-spend of the pipeline's one expensive step that the
    # compare-and-swap exists to prevent. There is no key that identifies a call
    # after the fact; the statement's own output is the only honest answer.
    #
    # `synchronize_session=False` because the identity map is refreshed by the
    # re-select below (`populate_existing`), and letting the ORM pick its own
    # strategy for a `RETURNING` update means two mechanisms doing one job.
    granted = set(
        session.execute(
            update(Document)
            .where(Document.id.in_(candidates), _claimable(cutoff))
            .values(
                extraction_state=ExtractionState.CLAIMED,
                extraction_claimed_at=moment,
                extraction_claimed_by=worker_id,
                # Counted on the way out, not on a reported failure — see the module
                # docstring. A worker that never reports anything still burns one.
                extraction_attempts=Document.extraction_attempts + 1,
            )
            .returning(Document.id)
            .execution_options(synchronize_session=False)
        )
        .scalars()
        .all()
    )
    session.flush()
    if not granted:
        return []

    # Re-selected rather than returned from the statement above, because a claim has
    # to hand back whole `Document` objects and a `RETURNING` of every column would
    # bypass the identity map — leaving a caller holding two versions of one row.
    claimed = list(
        session.execute(
            select(Document)
            .where(Document.id.in_(granted))
            .order_by(Document.id)
            .execution_options(populate_existing=True)
        ).scalars()
    )
    return claimed


# ---------------------------------------------------------------------------
# The submit door
# ---------------------------------------------------------------------------


def record_text(
    session: Session,
    *,
    document: Document,
    extractor: str,
    pages: Sequence[str],
    now: datetime | None = None,
) -> DocumentText:
    """Record extracted text for a document, replacing whatever was there.

    **Not restricted to the lease holder, and not restricted to `CLAIMED`.** A
    worker whose lease expired while it was legitimately grinding through a
    400-page PDF still holds correct text, and discarding it to protect a
    bookkeeping nicety would throw away the only expensive step in the pipeline.
    Idempotency by content address is what makes that safe: a late submission and a
    duplicate submission both land the same row.

    A document that was `NOT_APPLICABLE` is accepted too — a submission is evidence
    that *something* could read it, which is precisely how a future OCR pass over a
    photographed marking would arrive. `NOT_APPLICABLE` means "not queued for
    automatic extraction", never "text is forbidden here".
    """
    moment = now or utcnow()
    char_count = sum(len(page) for page in pages)
    if char_count > MAX_EXTRACTED_CHARS:
        raise ExtractionError(
            f"{char_count} extracted characters exceeds the {MAX_EXTRACTED_CHARS} limit",
            reason="text_too_large",
        )

    document.page_count = len(pages)
    document.text_char_count = char_count
    # Derived here, never taken from the caller. See the module docstring.
    document.text_low_confidence = is_low_confidence(char_count, len(pages))
    document.extractor = extractor
    document.extracted_at = moment
    document.extraction_state = ExtractionState.EXTRACTED
    document.extraction_claimed_at = None
    # The lease is released but its holder is left recorded: "who last worked on
    # this" survives, and nothing branches on the field.
    document.extraction_error = None
    _index(session, document, _PAGE_SEPARATOR.join(page for page in pages if page.strip()))
    session.flush()
    return describe(session, document)


def record_failure(session: Session, *, document: Document, error: str) -> DocumentText:
    """Record that an attempt failed, and decide whether to offer it again.

    Back to `PENDING` while attempts remain, `FAILED` once they are spent. The
    count was already incremented by the claim, so a worker reporting a failure
    does not double-count itself — reporting is a courtesy that shortens the wait
    from a lease expiry to nothing, not the mechanism that makes progress.

    Takes no timestamp, unlike `record_text`: `extracted_at` dates the text in the
    index and a failure produced none, so there is nothing here a clock would date.

    Any text already in the index is **left alone**. A failed re-run with a better
    extractor must not make search worse than it was before somebody tried to
    improve it.
    """
    document.extraction_error = error
    document.extraction_claimed_at = None
    if document.extraction_attempts >= MAX_EXTRACTION_ATTEMPTS:
        document.extraction_state = ExtractionState.FAILED
    else:
        document.extraction_state = ExtractionState.PENDING
    session.flush()
    return describe(session, document)


def requeue(session: Session, *, document: Document) -> DocumentText:
    """Offer a document to the queue again, from zero attempts.

    The door for two things: retrying a `FAILED` document after the cause is fixed,
    and **re-reading an already-extracted one with a better extractor** — ADR
    0005's `PyPdfExtractor` → `DoclingExtractor` upgrade path, which needs no new
    machinery because it is just this.

    The existing text is deliberately **not** cleared. It stays searchable until
    the better run replaces it, because clearing it first would make search
    strictly worse for as long as the queue takes to come round, in exchange for
    nothing.
    """
    document.extraction_state = initial_state(document.media_type)
    document.extraction_attempts = 0
    document.extraction_claimed_at = None
    document.extraction_claimed_by = None
    document.extraction_error = None
    session.flush()
    return describe(session, document)


def clear_index(session: Session, document: Document) -> None:
    """Drop whatever `datasheet_fts` holds at this document's id.

    Called by `app.services.documents.store_document` for every **new** row, and
    that is the point: `rowid` is `documents.id`, SQLite reuses an id after a
    delete, and the index has neither a foreign key nor a delete trigger to notice.
    A fresh document could therefore be handed an id whose index entry outlived the
    document it was extracted from — and then report that document's text through
    `describe`, while `extraction_state` still said `pending`.

    Deliberately not a repair for the *stale entry* in general: `reconcile` is that,
    and it needs somebody to run it. This runs on the path that creates the
    collision, so the collision cannot survive it.
    """
    session.execute(_FTS_DELETE, {"document_id": document.id})


def _index(session: Session, document: Document, body: str) -> None:
    """Replace this document's entry in `datasheet_fts`.

    Delete-then-insert because FTS5 has no upsert. Both statements are in the
    caller's transaction, so a reader never sees the gap and a rolled-back
    submission leaves the previous text intact.
    """
    session.execute(_FTS_DELETE, {"document_id": document.id})
    session.execute(_FTS_INSERT, {"document_id": document.id, "body": body})


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reconciliation:
    """What `reconcile` found disagreeing, and repaired."""

    #: Rows claiming `EXTRACTED` with no entry in the index. Re-queued: the blob is
    #: still there, so the text is one worker run away.
    missing_text: int
    #: Entries in the index whose document has **recorded no extraction at all** —
    #: `extracted_at IS NULL`, or no row at that id. Deleted, because `documents` is
    #: the authority on what exists and a stale entry makes a search hit resolve to
    #: a document that will report having no text.
    #:
    #: Emphatically **not** "every entry whose document is not `EXTRACTED`", which
    #: is what this used to be: `requeue` and `record_failure` both leave text
    #: indexed on purpose while moving the state away from `EXTRACTED`, so that
    #: predicate deleted exactly the text those two promise to keep.
    orphaned_text: int


def reconcile(session: Session) -> Reconciliation:
    """Make `extraction_state` and `datasheet_fts` agree, and say what was wrong.

    The state column and the index are two facts that ought to be one, and this
    codebase's rule for that is the same everywhere — `qty_milli_cached`,
    `id_path`, `qty_reserved_milli_cached`: the derived copy must be rebuildable in
    one pass, so that a bug in the writer is a stale read and never data loss.
    Here the blob is the authority behind both, which is why the repair for a
    missing entry is to requeue rather than to invent anything.

    **The orphan test is "nothing was ever extracted at this id", not "this
    document is not `EXTRACTED` right now."** Three documented states hold text
    while not being `EXTRACTED`, all reachable from public routes: a document
    requeued for a better extractor, one currently claimed, and one whose better
    run failed. `requeue` and `record_failure` both say in as many words that the
    old text stays searchable, and repair code that contradicts them is worse than
    none, because it runs unattended. For an image it was not even recoverable —
    `NOT_APPLICABLE` is never claimable, so OCR text deleted here could never be
    produced again.
    """
    indexed = set(session.execute(text("SELECT rowid FROM datasheet_fts")).scalars().all())
    rows = session.execute(
        select(Document).where(Document.extraction_state == ExtractionState.EXTRACTED)
    ).scalars()

    missing = 0
    for document in rows:
        if document.id not in indexed:
            requeue(session, document=document)
            missing += 1

    # `extracted_at` is the one column that means "text was recorded for this row",
    # and only `record_text` sets it — neither `requeue` nor `record_failure`
    # clears it, which is precisely why it survives the states above. An id with no
    # document at all is absent from this set too, so a reused rowid's inherited
    # entry is still swept.
    with_recorded_text = set(
        session.execute(select(Document.id).where(Document.extracted_at.is_not(None)))
        .scalars()
        .all()
    )
    orphans = indexed - with_recorded_text
    for rowid in sorted(orphans):
        session.execute(_FTS_DELETE, {"document_id": rowid})
    session.flush()
    return Reconciliation(missing_text=missing, orphaned_text=len(orphans))


#: Exported so a caller reading a count out of `status_counts` does not have to
#: import the model layer to name a state.
__all__ = [
    "EXTRACTABLE_MEDIA_TYPES",
    "LEASE_SECONDS",
    "LOW_CONFIDENCE_CHARS_PER_PAGE",
    "MAX_EXTRACTED_CHARS",
    "MAX_EXTRACTION_ATTEMPTS",
    "DocumentText",
    "ExtractionError",
    "ExtractionState",
    "Reconciliation",
    "chars_per_page",
    "claim",
    "clear_index",
    "describe",
    "expire_abandoned",
    "initial_state",
    "is_low_confidence",
    "reconcile",
    "record_failure",
    "record_text",
    "requeue",
    "status_counts",
    "text_of",
]
