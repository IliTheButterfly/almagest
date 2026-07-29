"""Documents as rows: storing one, and attaching it to something.

`app.services.blobstore` owns the bytes; this module owns `documents` and
`document_links`, and the two invariants that only exist once a database is
involved.

## The blob is written before the row, never the other way round

Either order can be interrupted, so the question is which orphan you want.

* Blob first, row lost: a file nobody references. Harmless, greppable, and
  **self-healing** — the next upload of those bytes finds the file already there,
  dedups onto it, and writes the row that was missing.
* Row first, blob lost: a `documents` row whose `storage_path` points at nothing.
  Every read of it is a 500 forever, the part detail screen offers a datasheet
  that cannot open, and no later upload repairs it because the row already exists
  so nothing re-attempts the write.

The second is unrecoverable without manual surgery, so the order is blob, then
row. This is the same reasoning the ledger uses for writing history before
touching a cache: prefer the failure that a retry fixes.

## Losing the race to insert the row is an outcome, not an error

The lookup and the insert are two statements, route handlers are `def` (so FastAPI
runs them in a threadpool), and pysqlite holds no read transaction across the
lookup — so two uploads of the same *new* file genuinely interleave, and the
loser used to get a 500 from `UNIQUE(sha256)`. That contradicted the route
module's own claim that the content address is the idempotency key "across
devices and across restarts", in exactly the case it names: a retry firing while
the first request is still in flight. So the insert runs in a savepoint and the
violation is answered by re-reading the winner's row. Recovery is *possible* only
because the row is content-addressed: there is one correct answer and both
requests want it.

## Exactly one primary per (entity, role)

`docs/PLAN.md`: `GET /api/parts/{id}/datasheet` redirects to *the* primary. **No
constraint can express that.** A partial unique index would give at most one,
which is the easy half; the half that actually breaks is *at least* one, and it
breaks by detaching the primary from a part that still has three other sheets —
after which the redirect 404s while the datasheets are all still there.

So it is maintained here, in the two places that can disturb it, exactly as
`app.services.shortid._make_primary` maintains it for `object_ids`:

* `attach` demotes the siblings of a new primary, and **forces** the first link
  in a role to be primary regardless of what the caller asked for, because a role
  whose only link is non-primary is the 404-with-a-datasheet-present state.
* `detach` promotes the oldest remaining sibling of a primary it removes.

The rule is therefore "a role with any links has exactly one primary", and that
is what `tests/integration/test_documents.py` asserts after every mutation.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.documents import Document, DocumentLink
from app.models.enums import DocumentKind, DocumentRole, EntityType
from app.services import blobstore, document_text
from app.services.blobstore import BlobError


@dataclass(frozen=True)
class StoredDocument:
    """The outcome of an upload, with both no-op flags reported separately.

    They can differ, and the difference is the recovery path above: an orphan blob
    from an interrupted upload gives `created=True, deduplicated=True` — the file
    was already there, the row is new.
    """

    document: Document
    #: A `documents` row was inserted.
    created: bool
    #: The bytes were already on disk, so nothing was written.
    deduplicated: bool


def store_document(
    session: Session,
    *,
    data: bytes,
    media_type: str,
    kind: DocumentKind,
    source_url: str | None = None,
    original_filename: str | None = None,
    claimed_sha256: str | None = None,
) -> StoredDocument:
    """Store bytes and return the document they address, new or existing.

    `claimed_sha256` is checked, never trusted: the digest is always computed from
    the bytes, and a mismatch is a refusal rather than a preference for one of the
    two. A client that knows what it is sending gets end-to-end integrity out of
    it — a truncated upload over flaky wifi is otherwise indistinguishable from a
    different document, since a truncated PDF still hashes to *something* valid
    and would be stored as a perfectly well-formed row.

    **Re-uploading an existing document changes nothing about it.** Not the kind,
    not the source URL, not the filename. That keeps "the second upload is a
    no-op" a testable statement instead of a mostly-true one; merging new metadata
    onto an existing row is a deliberate edit, and a silent merge would let a bad
    `kind` on one re-upload rewrite a good one.
    """
    # Validated before it is compared, so a `../` claim is refused as a bad digest
    # rather than merely failing to match.
    claimed = blobstore.validate_sha256(claimed_sha256) if claimed_sha256 is not None else None

    stored = blobstore.store(data, media_type=media_type)

    if claimed is not None and claimed != stored.sha256:
        raise BlobError(
            f"claimed sha256 {claimed} but the bytes hash to {stored.sha256}",
            reason="hash_mismatch",
        )

    existing = by_sha256(session, stored.sha256)
    if existing is not None:
        return StoredDocument(document=existing, created=False, deduplicated=stored.deduplicated)

    document = Document(
        sha256=stored.sha256,
        kind=kind,
        # The **canonical** type the store resolved, never the raw parameter. The
        # raw string reaches a response header, and one containing CR/LF or a
        # non-latin-1 character makes every later GET of this document fail in the
        # server's header writer — permanently, since re-uploading the same bytes
        # is documented (below) to change nothing about an existing row. See
        # `blobstore.canonical_media_type`.
        media_type=stored.media_type,
        byte_size=stored.byte_size,
        storage_path=stored.storage_path,
        source_url=source_url,
        original_filename=original_filename,
        # The only place a `documents` row is created, so the only place this
        # decision is made. A PDF joins the extraction queue; a tray photograph is
        # `NOT_APPLICABLE` and never appears in it. **Nothing here opens the file** —
        # the state is read off the declared media type, which is the whole reason
        # the API can queue work it has no library to do.
        extraction_state=document_text.initial_state(stored.media_type),
    )

    try:
        # A savepoint, so the unique violation below rolls back **only this insert**
        # and leaves the caller's transaction (and any attach it is about to make)
        # usable. Without one, recovering would mean rolling back the request.
        with session.begin_nested():
            session.add(document)
            session.flush()
    except IntegrityError:
        # Check-then-insert, and route handlers are `def`, so two threads sit in the
        # window between the lookup above and this flush: a retry firing while the
        # first request is still in flight, or a double-tap in the PWA. Both got a
        # 500 with `UNIQUE constraint failed: documents.sha256`, which contradicts
        # this module's own claim that the content address is the idempotency key.
        #
        # The winner's row is the right answer, so re-read it rather than inventing
        # one. The re-read cannot come back empty for the constraint we violated,
        # and a violation of some *other* constraint is a real bug — so `raise` is
        # the else branch instead of a second attempt at the same insert.
        loser = by_sha256(session, stored.sha256)
        if loser is None:
            raise
        return StoredDocument(document=loser, created=False, deduplicated=stored.deduplicated)

    # `datasheet_fts.rowid` **is** `documents.id`, which SQLite hands out again
    # after a delete (the column is a rowid alias with no `AUTOINCREMENT`), and the
    # index has no foreign key and no delete trigger. So a fresh row can be handed
    # an id whose index entry outlived the document it was extracted from — after
    # which this document reports the other one's text, while `extraction_state`
    # still says `pending`. Clearing at creation is the cheap end of the fix: it
    # runs once per genuinely new document and needs no schema change.
    document_text.clear_index(session, document)
    return StoredDocument(document=document, created=True, deduplicated=stored.deduplicated)


def by_sha256(session: Session, sha256: str) -> Document | None:
    """Look a document up by its address. Validates before querying, so a
    traversal payload is refused here too rather than merely missing."""
    digest = blobstore.validate_sha256(sha256)
    return session.execute(select(Document).where(Document.sha256 == digest)).scalar_one_or_none()


def attach(
    session: Session,
    *,
    document: Document,
    entity_type: EntityType,
    entity_pk: int,
    role: DocumentRole = DocumentRole.DATASHEET,
    is_primary: bool = True,
) -> tuple[DocumentLink, bool]:
    """Link a document to an entity in a role. Returns the link and whether it is new.

    An upsert, because the unique key says a second identical link is a duplicate
    rather than a second attachment — so re-attaching is how a client promotes an
    existing link to primary, and needs no separate route.
    """
    existing = session.execute(
        select(DocumentLink).where(
            DocumentLink.document_id == document.id,
            DocumentLink.entity_type == entity_type,
            DocumentLink.entity_pk == entity_pk,
            DocumentLink.role == role,
        )
    ).scalar_one_or_none()

    # The first link in a role is primary whatever the caller said. A role whose
    # only link is not primary is the state where `/datasheet` 404s while a
    # datasheet is attached and visible in the list beside it.
    first_in_role = (
        primary_link(session, entity_type=entity_type, entity_pk=entity_pk, role=role) is None
    )
    promote = is_primary or first_in_role

    if existing is not None:
        if promote:
            _make_primary(session, existing)
        return existing, False

    link = DocumentLink(
        document_id=document.id,
        entity_type=entity_type,
        entity_pk=entity_pk,
        role=role,
        is_primary=promote,
    )
    session.add(link)
    session.flush()
    if promote:
        _make_primary(session, link)
    return link, True


@dataclass(frozen=True)
class Detachment:
    """What `detach` removed, and what it promoted to keep the invariant."""

    #: Links removed — one per role this document held for this entity. Zero means
    #: there was nothing attached, which a route reports as a 404 rather than as a
    #: successful no-op: a client that believes it removed something is entitled to
    #: be right.
    removed: int
    #: One per role that lost its primary and still has links. Reported rather
    #: than left for the caller to re-read, because the invariant was just
    #: repaired and a client that has to guess whether it was will guess wrong.
    promoted: list[DocumentLink]


def detach(
    session: Session,
    *,
    document: Document,
    entity_type: EntityType,
    entity_pk: int,
) -> Detachment:
    """Remove every link between this document and this entity, in every role.

    Detaching does **not** delete the document or its blob. One family PDF serves
    many MPNs, so a part dropping its link says nothing about whether the file is
    still wanted; reclaiming unreferenced blobs is a sweep, not a side effect of a
    click.
    """
    links = list(
        session.execute(
            select(DocumentLink)
            .where(
                DocumentLink.document_id == document.id,
                DocumentLink.entity_type == entity_type,
                DocumentLink.entity_pk == entity_pk,
            )
            .order_by(DocumentLink.id)
        ).scalars()
    )
    orphaned_roles = [DocumentRole(link.role) for link in links if link.is_primary]
    for link in links:
        session.delete(link)
    session.flush()

    promoted: list[DocumentLink] = []
    for role in orphaned_roles:
        # Oldest first: the deterministic choice, and the one that matches "the
        # sheet you attached first is the fallback". Anything ranked would need a
        # ranking nobody has entered.
        replacement = session.execute(
            select(DocumentLink)
            .where(
                DocumentLink.entity_type == entity_type,
                DocumentLink.entity_pk == entity_pk,
                DocumentLink.role == role,
            )
            .order_by(DocumentLink.id)
            .limit(1)
        ).scalar_one_or_none()
        if replacement is not None:
            _make_primary(session, replacement)
            promoted.append(replacement)
    return Detachment(removed=len(links), promoted=promoted)


def primary_link(
    session: Session,
    *,
    entity_type: EntityType,
    entity_pk: int,
    role: DocumentRole = DocumentRole.DATASHEET,
) -> DocumentLink | None:
    """The one link a role resolves to, or None when the role has no links.

    `scalar_one_or_none` on purpose: if the invariant were ever broken so that two
    links claimed the same role, this raises instead of silently picking one. A
    redirect that quietly serves an arbitrary datasheet is worse than an error
    that says the data is wrong.
    """
    return session.execute(
        select(DocumentLink).where(
            DocumentLink.entity_type == entity_type,
            DocumentLink.entity_pk == entity_pk,
            DocumentLink.role == role,
            DocumentLink.is_primary.is_(True),
        )
    ).scalar_one_or_none()


def links_for(
    session: Session, *, entity_type: EntityType, entity_pk: int
) -> list[tuple[DocumentLink, Document]]:
    """Every document attached to one entity, primaries first then oldest first.

    Joined rather than lazy-loaded so listing a part's documents is one query
    regardless of how many there are — the same reason nothing in this codebase
    reads a balance by summing a ledger.
    """
    rows = session.execute(
        select(DocumentLink, Document)
        .join(Document, Document.id == DocumentLink.document_id)
        .where(
            DocumentLink.entity_type == entity_type,
            DocumentLink.entity_pk == entity_pk,
        )
        .order_by(DocumentLink.role, DocumentLink.is_primary.desc(), DocumentLink.id)
    ).all()
    return [(link, document) for link, document in rows]


@dataclass(frozen=True)
class ScrubReport:
    """What `scrub` found, with the two failures kept apart."""

    #: Rows examined. Complete, unlike a drift sample: a scrub that read half the
    #: store and said nothing was wrong would be worse than no scrub.
    checked: int
    #: Addresses whose blob is gone. Recoverable — the row still records what the
    #: file was, so re-uploading the same bytes repairs it and returns
    #: `created=False`.
    missing: tuple[str, ...]
    #: Addresses whose blob is present but does not hash to its own name. **The
    #: dangerous one**: it is served as authoritative, cached `immutable`, and every
    #: future upload of the correct bytes dedups onto it, so nothing repairs it by
    #: itself.
    corrupt: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        return not self.missing and not self.corrupt


def scrub(session: Session) -> ScrubReport:
    """Re-hash every stored blob against its own name. Reads only; repairs nothing.

    The scrub job `app.services.blobstore`'s module docstring defers to — "it
    belongs in a scrub job, which is what `verify` exists for" — which did not
    exist, leaving bit rot and a half-restored volume undetectable. Run nightly,
    beside `app.db.maintenance.check_lot_balance_drift`, and for the same reason:
    a derived copy is only safe to trust if something checks it.

    **It does not delete or rewrite anything**, deliberately. A blob is
    re-fetchable — it is a PDF that exists on a manufacturer's website — while the
    row's metadata, its links and its extracted text are not, so turning one bad
    sector into a deleted document would trade a recoverable failure for an
    unrecoverable one. That is the same asymmetry the blob-before-row write order
    is chosen for.

    Every blob is read in full, so this is I/O-bound in the size of the store and
    is a job, never an API path: the single API replica must not spend a request
    hashing a gigabyte of datasheets.
    """
    missing: list[str] = []
    corrupt: list[str] = []
    checked = 0
    rows = session.execute(select(Document).order_by(Document.id)).scalars()
    for document in rows:
        checked += 1
        # `path_for` on the recorded path, not `blob_path` on the digest: a row
        # written under an older fanout rule must be checked where it actually
        # lives, or the scrub would report the whole store corrupt after a layout
        # change. The hashing itself is `blobstore`'s, not a second copy of it.
        path = blobstore.path_for(document.storage_path)
        if not path.is_file():
            missing.append(document.sha256)
        elif not blobstore.verify_stored(path, document.sha256):
            corrupt.append(document.sha256)
    return ScrubReport(checked=checked, missing=tuple(missing), corrupt=tuple(corrupt))


def _make_primary(session: Session, link: DocumentLink) -> None:
    """Make `link` the one its (entity, role) resolves to, demoting the rest.

    Same shape as `app.services.shortid._make_primary`, and for the same reason:
    "exactly one" is not expressible as a constraint, so it is a single statement
    that demotes every sibling followed by a promotion of this row — never a
    read-modify-write per sibling, which is where a partial failure would leave
    two primaries.
    """
    session.execute(
        update(DocumentLink)
        .where(
            DocumentLink.entity_type == link.entity_type,
            DocumentLink.entity_pk == link.entity_pk,
            DocumentLink.role == link.role,
            DocumentLink.id != link.id,
        )
        .values(is_primary=False)
    )
    link.is_primary = True
    session.flush()
