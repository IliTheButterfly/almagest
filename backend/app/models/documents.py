"""The content-addressed document store: one row per *file*, many links to it.

`docs/PLAN.md`: `data/datasheets/{sha256[0:2]}/{sha256[2:4]}/{sha256}.pdf`,
git-style fanout, **hash computed before the write so dedup is free**, and
"M:N because one family PDF covers many MPNs". Both halves of that sentence are
load-bearing here.

*Content-addressed* means the sha256 **is** the identity. There is no `short_id`
on a document and nothing mints one — `EntityType.DOCUMENT` exists for the shared
ID space but stays unused by this phase, because a printed label on a PDF is not
a thing, and the address is already stable, already unique, and already the
filename. It is also the retry key ADR 0005 needs: an extraction worker that dies
mid-run re-claims work by hash, exactly as `client_op_id` does for movements.

*M:N* means the primary-datasheet question cannot live on `documents`. Two parts
sharing one blob is the common case for a family, and one of them may treat that
sheet as its specification while another only references it, so "is this the
primary" is a property of the **link** and not of the file. See
`app.services.documents.attach` for how exactly-one is maintained, and
`DocumentRole` for why `kind` and `role` are both here.

**Nothing in this module or in the API opens the file.** `page_count` is NULL
until the out-of-process extractor fills it in (ADR 0005: the API owns storage
and search and never parses a PDF), which is why it is nullable on a column that
looks like it should be derivable. A document with no page count and no extracted
text is a first-class state, not a failure.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.base import TimestampMixin
from app.models.enums import DocumentKind, DocumentRole, EntityType, ExtractionState
from app.models.types import StrEnumType, UtcDateTime, utcnow

#: Hex sha256. Fixed width, and the *only* value that ever reaches a filesystem
#: path — `app.services.blobstore.validate_sha256` is the gate.
SHA256_LENGTH = 64

#: `{aa}/{bb}/{sha256}{suffix}` needs 74 characters for a `.pdf`. The width
#: documents intent (SQLite ignores it) and leaves room for a longer suffix.
_STORAGE_PATH_LENGTH = 255


class Document(Base, TimestampMixin):
    """One stored file, addressed by the sha256 of its bytes.

    Every column except `page_count` is known at upload time without opening the
    file: the digest and size come from the bytes, `media_type` and `kind` from
    the caller, `storage_path` from the fanout rule. That is what keeps the API
    free of a PDF parser.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The address. UNIQUE is the dedup mechanism, not a sanity check: the upload
    #: path looks a document up by this column *before* deciding whether to write
    #: anything, so re-uploading a family datasheet for the twelfth part in the
    #: family is a read.
    sha256: Mapped[str] = mapped_column(String(SHA256_LENGTH), nullable=False, unique=True)

    kind: Mapped[str] = mapped_column(StrEnumType(DocumentKind), nullable=False)

    #: IANA media type, from `app.services.blobstore.MEDIA_TYPES`. Named
    #: `media_type` rather than `docs/PLAN.md`'s `mime` only because that is what
    #: HTTP, Starlette and every client library call it; same column.
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)

    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)

    #: NULL until something that can read the format says otherwise — see the
    #: module docstring. **Do not** derive it in an API path.
    page_count: Mapped[int | None] = mapped_column(Integer)

    #: Where it came from, when it was fetched rather than uploaded. Kept because
    #: "external datasheet URLs rot within a few years" (`docs/PLAN.md`) — once
    #: the URL is dead this is the only record of what the local copy *is*, and
    #: it is also how a re-fetch is recognised as the same document.
    source_url: Mapped[str | None] = mapped_column(Text)

    #: Relative to `Settings.datasheet_dir`, POSIX separators, derived from
    #: `sha256` by the fanout rule. Stored rather than always recomputed so the
    #: rule can change (a deeper fanout, a moved blob, a different suffix) without
    #: invalidating rows written under the old one — the same reason
    #: `label_prints` records what was printed instead of re-deriving it.
    storage_path: Mapped[str] = mapped_column(String(_STORAGE_PATH_LENGTH), nullable=False)

    #: What the human called it. Content addressing throws the name away, and
    #: "STM32F103.pdf" is what someone recognises in a list of hashes. Never used
    #: to build a path, and sanitised before it reaches a header — see
    #: `app.api.routes.documents.content_disposition`.
    original_filename: Mapped[str | None] = mapped_column(String(255))

    # -- The extraction queue ------------------------------------------------
    #
    # Eight columns rather than a `document_extractions` table, because ADR 0005
    # says the queue is "a plain index, not a table" and these are the index.
    # `Index("ix_documents_extraction_queue", ...)` below is the whole query plan:
    # claiming reads one index range on one table and joins nothing.
    #
    # The extracted **text** is not here. It goes into `datasheet_fts(rowid =
    # documents.id)`, which the FTS migration created for exactly this and which
    # says so ("its rowid is the future documents.id ... datasheet text arrives
    # from an extraction pipeline"). One copy, in the only structure that can
    # search it — and it is legitimately a cache rather than storage, because the
    # blob is the authority and extraction is a pure function of it, so a lost
    # index is a re-run and never lost data.

    #: See `ExtractionState`. `PENDING` for a stored-but-unread PDF, which is an
    #: ordinary state and never an error.
    extraction_state: Mapped[str] = mapped_column(
        StrEnumType(ExtractionState),
        nullable=False,
        default=ExtractionState.PENDING,
        server_default=ExtractionState.PENDING.value,
    )

    #: Incremented **at claim time, not at failure time.** That is the load-bearing
    #: half: a worker that is `SIGKILL`ed, segfaults in a parser, or loses power
    #: never reports anything, so an attempt counted only on a reported failure
    #: would count zero and the same document would be re-served until the end of
    #: time. Counting the claim makes a document that reliably kills its worker
    #: run out of attempts on its own.
    extraction_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: When the current lease started. NULL unless `extraction_state` is
    #: `CLAIMED`. Compared against `LEASE_SECONDS` to decide that a holder is
    #: gone — a *lease*, not a lock, because nothing can be relied upon to
    #: release a lock held by a process that no longer exists.
    extraction_claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    #: Which worker holds it. Diagnostics only — no code branches on it, and a
    #: submission is **not** required to come from the holder (see
    #: `app.services.document_text.record_text`). Worth storing anyway: "which
    #: pod has been sitting on this for an hour" is otherwise unanswerable.
    extraction_claimed_by: Mapped[str | None] = mapped_column(String(64))

    #: When the text that is in the index now was submitted.
    extracted_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    #: Which `app.services.extractors.Extractor` produced it — `pypdf`,
    #: `docling`, an OCR pass. Recorded because it is the only way to answer "was
    #: this read by the cheap extractor?", which is precisely the query that
    #: drives a re-run once a better one exists. Without it, upgrading the
    #: extractor means re-extracting everything or nothing.
    extractor: Mapped[str | None] = mapped_column(String(32))

    #: Characters of extracted text, and the pages they came from
    #: (`page_count` above). Stored as the two counts rather than as a ratio so
    #: the ratio is computed in exactly one place —
    #: `app.services.document_text.chars_per_page`.
    text_char_count: Mapped[int | None] = mapped_column(Integer)

    #: **The stored judgement**, and the one column a caller should read instead
    #: of re-deriving anything: `extracted-chars-per-page ≈ 0` is both the OCR
    #: escalation signal and the low-confidence flag (`docs/PLAN.md`, ADR 0005),
    #: and it must be recorded rather than recomputed by every consumer that
    #: happens to remember the threshold.
    #:
    #: **Nullable on purpose: NULL means no judgement has been made.** A
    #: never-extracted document is neither trusted nor distrusted, and a
    #: `False` default would make an unread scanned datasheet look positively
    #: vouched for — the exact "empty and silently trusted" outcome the flag
    #: exists to prevent.
    #:
    #: Derived by the API from the submitted text, **never accepted from the
    #: worker**: see `app.services.document_text.record_text`.
    text_low_confidence: Mapped[bool | None] = mapped_column(Boolean)

    #: Why the last attempt failed, verbatim from the worker. Free text because
    #: the failures are a parser's exception strings and nobody can enumerate
    #: them; it is read by a human looking at a health check, not branched on.
    extraction_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # The work queue, in one index. `extraction_attempts` sits second because
        # the claim orders by it — never-tried documents before retries, so one
        # poison PDF cannot starve a fresh upload behind it — and `id` third to
        # make the order total and therefore the claim deterministic.
        Index("ix_documents_extraction_queue", "extraction_state", "extraction_attempts", "id"),
    )


class DocumentLink(Base):
    """One reason one entity points at one document.

    Polymorphic by (`entity_type`, `entity_pk`) rather than by a real foreign key,
    for the same reason `object_ids` and `barcode_aliases` are: the referent may
    be a part, a lot, a location or a project, and Phase 3's tray images attach to
    a *lot* while Phase 5's datasheets attach to a *part*. A column per target
    type would mean a migration per phase.

    The cost is honest and worth stating: nothing at the database level stops a
    link to a deleted part. Deletes here are rare and mediated (`parts` is not
    deleted by any route today), and the alternative — six nullable FK columns
    with a check that exactly one is set — needs the `CHECK` this schema forbids.
    """

    __tablename__ = "document_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: CASCADE: deleting the file's row deletes the pointers to it. There is no
    #: meaningful "link to a document that no longer exists".
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    entity_type: Mapped[str] = mapped_column(StrEnumType(EntityType), nullable=False)
    entity_pk: Mapped[int] = mapped_column(Integer, nullable=False)

    role: Mapped[str] = mapped_column(StrEnumType(DocumentRole), nullable=False)

    #: The one this role resolves to — what `GET /api/parts/{id}/datasheet`
    #: redirects to. **No constraint can express "exactly one per (entity, role)"**
    #: in SQLite, so it is maintained in `app.services.documents` (the pattern
    #: `app.services.shortid._make_primary` already uses for `object_ids`) and
    #: asserted in `tests/integration/test_documents.py`. A partial unique index
    #: on `is_primary = 1` would enforce *at most* one, which is the half that
    #: never breaks; the half that breaks is *at least* one, after the primary is
    #: detached and nothing is promoted in its place.
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # Linking the same file to the same entity in the same role twice is a
        # duplicate, not a second link, so attach is an upsert. All four columns
        # are NOT NULL, so — unlike a constraint over a nullable column, where
        # SQLite treats NULLs as distinct — this really does constrain what it
        # appears to.
        UniqueConstraint(
            "document_id",
            "entity_type",
            "entity_pk",
            "role",
            name="uq_document_links_binding",
        ),
        # Both reads this table has: "every document attached to this part"
        # (prefix of two columns) and "the primary for this role" (all three).
        # `is_primary` is deliberately not in the index — at a handful of rows per
        # entity it filters nothing, and leaving it out means promoting a
        # different link does not touch the index.
        Index("ix_document_links_entity", "entity_type", "entity_pk", "role"),
        # No index on `document_id`: it is the leading column of the unique
        # constraint above, which already serves "which parts use this datasheet".
    )
