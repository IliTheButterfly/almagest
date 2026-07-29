"""The content-addressed document store: dedup, atomicity, traversal, primaries.

## The PDF fixtures are hand-assembled, not produced by a library

`_pdf()` below writes the ~20 bytes of a PDF header and trailer by hand. There is
no `pypdf` import anywhere in this file, and that is a requirement rather than a
convenience: per `docs/adr/0005-extraction-runs-outside-the-api.md` the API never
parses a PDF, so a test suite that needed a PDF library to exercise the *store*
would be asserting a dependency the store does not have. Nothing here validates
PDF structure — `app.services.blobstore` checks five bytes of magic and stops —
so a real document would only make the fixtures slower to read.

The extraction-side tests, when they arrive, are the ones that need the
`datasheets` extra.

## What is asserted about the primary rule, and why it needs asserting

"Exactly one primary per (part, role)" is not expressible as a SQLite constraint
(see the migration's own note), so `_assert_one_primary_per_role` re-derives it
from the table after every mutation in this file. It is the only guard there is.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.documents import Document, DocumentLink
from app.models.enums import DocumentKind, DocumentRole, EntityType
from app.services import blobstore, documents
from app.services.blobstore import BlobError
from tests.factories import make_part

PDF = "application/pdf"


def _pdf(body: bytes = b"one") -> bytes:
    """A byte string that begins like a PDF and is otherwise unique per `body`."""
    return b"%PDF-1.7\n" + body + b"\n%%EOF\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _upload(
    client: TestClient, data: bytes = _pdf(), *, media_type: str = PDF, **params: object
) -> Response:
    """Raw body, metadata in the query string — no multipart, so no
    `python-multipart` in the default install. See the route module's docstring."""
    return client.post(
        "/api/documents",
        content=data,
        params={"media_type": media_type, **params},
        headers={"Content-Type": "application/octet-stream"},
    )


def _assert_one_primary_per_role(db: Session) -> None:
    """Every (entity, role) with links has exactly one primary.

    Derived from the table rather than trusted, because no constraint enforces it
    and the failure it guards against is invisible: zero primaries means
    `/api/parts/{id}/datasheet` 404s while the datasheets are still listed beside
    it, and two means the redirect serves an arbitrary one of them.
    """
    rows = db.execute(
        select(
            DocumentLink.entity_type,
            DocumentLink.entity_pk,
            DocumentLink.role,
            func.sum(DocumentLink.is_primary),
        ).group_by(DocumentLink.entity_type, DocumentLink.entity_pk, DocumentLink.role)
    ).all()
    for entity_type, entity_pk, role, primaries in rows:
        assert primaries == 1, (
            f"{entity_type} {entity_pk} role {role} has {primaries} primary links, expected 1"
        )


# ---------------------------------------------------------------------------
# The blob store: layout, dedup, atomicity
# ---------------------------------------------------------------------------


def test_the_layout_is_the_documented_fanout() -> None:
    """`{sha[0:2]}/{sha[2:4]}/{sha}.pdf`, exactly as `docs/PLAN.md` specifies.

    Pinned rather than left implicit because the path is written into
    `documents.storage_path` and every stored row is a claim about it.
    """
    data = _pdf(b"layout")
    stored = blobstore.store(data, media_type=PDF)
    digest = _sha(data)

    assert stored.sha256 == digest
    assert stored.storage_path == f"{digest[0:2]}/{digest[2:4]}/{digest}.pdf"
    path = blobstore.blob_path(digest, ".pdf")
    assert path.read_bytes() == data
    assert path.parent.parent.parent == blobstore.root().resolve()


def test_the_second_store_of_the_same_bytes_writes_nothing() -> None:
    """Dedup is observable and is a genuine no-op, not an idempotent overwrite.

    The mtime is the assertion that matters: a store that rewrote identical bytes
    would pass a content comparison while still burning an fsync per re-upload and
    while briefly replacing a good blob with a temp file.
    """
    data = _pdf(b"twice")
    first = blobstore.store(data, media_type=PDF)
    assert first.deduplicated is False

    path = blobstore.blob_path(first.sha256, ".pdf")
    before = path.stat().st_mtime_ns

    second = blobstore.store(data, media_type=PDF)
    assert second.deduplicated is True
    assert second.sha256 == first.sha256
    assert path.stat().st_mtime_ns == before


def test_a_stored_blob_verifies_against_its_own_name() -> None:
    stored = blobstore.store(_pdf(b"verify"), media_type=PDF)
    assert blobstore.verify(stored.sha256, ".pdf") is True
    # And the check is real: corrupt the blob and it must fail.
    blobstore.blob_path(stored.sha256, ".pdf").write_bytes(_pdf(b"tampered"))
    assert blobstore.verify(stored.sha256, ".pdf") is False


def test_the_final_name_only_ever_appears_by_a_same_directory_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blob's real name is created by a rename, never written into directly.

    Asserted structurally, by watching the move, because the failure it prevents
    cannot be observed from inside the writing process: a reader that opens the
    final name **while** bytes are still being written to it sees a file that is
    short and whose name asserts its own sha256. Nothing downstream ever
    re-validates that, and every future upload of those bytes dedups onto the
    damaged copy, so this is the one corruption the store cannot detect or repair.

    Both properties matter and a write-in-place implementation breaks only one of
    them, so both are checked: the source is a **sibling** (same directory, so the
    move is a rename within one filesystem — which is what `os.replace` needs in
    order to be atomic at all), and the target does not exist yet at the moment of
    the move.
    """
    observed: dict[str, object] = {}
    real_replace = Path.replace

    def recording_replace(self: Path, target: object) -> None:
        observed["source"] = Path(str(self))
        observed["target_existed"] = Path(str(target)).exists()
        real_replace(self, target)

    # No `monkeypatch.undo()` here or below. The `monkeypatch` fixture is one
    # instance shared with every other fixture in this test, so undoing would also
    # revert `_isolate_datasheet_dir` and point the rest of the test at the repo's
    # real `data/datasheets`. Teardown restores everything anyway, and nothing after
    # this line calls `replace`.
    monkeypatch.setattr(Path, "replace", recording_replace)
    data = _pdf(b"atomic")
    stored = blobstore.store(data, media_type=PDF)

    target = blobstore.blob_path(stored.sha256, ".pdf")
    source = observed["source"]
    assert isinstance(source, Path)
    assert source != target, "the blob was written straight to its final name"
    assert source.suffix == ".part"
    assert source.parent == target.parent, "a cross-directory move is not atomic"
    assert observed["target_existed"] is False
    assert target.read_bytes() == data


def test_no_partial_write_survives_at_the_final_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed write leaves the store without a blob, never with a truncated one.

    Simulated by making the move fail, which is the last step: everything before
    it has already happened, so this is the worst-case interruption for the final
    name. The `.part` orphan is acceptable; a short file at a name that asserts its
    own sha256 is not, because every future upload of those bytes would dedup onto
    it and no read would ever notice.
    """
    data = _pdf(b"interrupted")
    target = blobstore.blob_path(_sha(data), ".pdf")

    def exploding_replace(self: Path, other: object) -> None:
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(Path, "replace", exploding_replace)
    with pytest.raises(OSError, match="simulated crash"):
        blobstore.store(data, media_type=PDF)

    assert not target.exists()
    # And no temp file is left behind by a *handled* failure either.
    assert list(target.parent.glob("*.part")) == []


def test_an_empty_document_is_refused() -> None:
    """The sha256 of nothing is a perfectly valid address, which is the problem."""
    with pytest.raises(BlobError) as caught:
        blobstore.store(b"", media_type=PDF)
    assert caught.value.reason == "empty_document"


def test_bytes_that_are_not_the_declared_format_are_refused() -> None:
    """`docs/PLAN.md`: external datasheet URLs rot. A dead one usually answers
    with an HTML error page under whatever content type was asked for, and stored
    unchecked that becomes a permanent datasheet that opens as a blank tab."""
    with pytest.raises(BlobError) as caught:
        blobstore.store(b"<html><title>404 Not Found</title>", media_type=PDF)
    assert caught.value.reason == "content_mismatch"


def test_an_unsupported_media_type_is_refused_rather_than_guessed() -> None:
    with pytest.raises(BlobError) as caught:
        blobstore.store(b"MZ\x90\x00", media_type="application/x-msdownload")
    assert caught.value.reason == "unsupported_media_type"


def test_an_oversized_document_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(blobstore, "MAX_DOCUMENT_BYTES", 16)
    with pytest.raises(BlobError) as caught:
        blobstore.store(_pdf(b"x" * 64), media_type=PDF)
    assert caught.value.reason == "document_too_large"


def test_images_share_the_store() -> None:
    """`docs/PLAN.md`'s `count_sessions` puts tray images in `documents` too, so a
    PDF-only store would need a second one for Phase 3 to land."""
    png = b"\x89PNG\r\n\x1a\n" + b"tray"
    stored = blobstore.store(png, media_type="image/png")
    assert stored.storage_path.endswith(".png")


# ---------------------------------------------------------------------------
# Path traversal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "../" * 12 + "etc/passwd",
        "..",
        "../../../../etc/shadow",
        "a" * 63,  # too short
        "a" * 65,  # too long
        "g" * 64,  # not hex
        # A trailing newline. `$` instead of `\Z` in the regex would have accepted
        # this, and so would a `.strip()` — a filename with a newline in it is
        # exactly the sort of thing that then breaks something further downstream.
        f"{'a' * 64}\n",
        f" {'a' * 64} ",
        f"{'a' * 64}/../../etc/passwd",
        "",
        "/etc/passwd",
        "\x00" + "a" * 63,
        # 64 characters exactly, so every length check passes and only the
        # character class stands between this and a `Path`.
        "../" * 21 + "x",
    ],
)
def test_only_64_hex_characters_ever_reach_a_path(payload: str) -> None:
    """`validate_sha256` is the single door into `blob_path`, so every one of these
    has to be refused *before* a `Path` exists — not merely fail to be found."""
    with pytest.raises(BlobError) as caught:
        blobstore.validate_sha256(payload)
    assert caught.value.reason == "invalid_sha256"

    with pytest.raises(BlobError):
        blobstore.blob_path(payload, ".pdf")


def test_a_stored_path_that_escapes_the_store_is_refused() -> None:
    """`documents.storage_path` is trusted data — and still checked.

    It reaches a filesystem call, and "trusted data reaching a filesystem call" is
    a combination with a poor record: the row could come from a future migration, a
    bulk import, or a bug, none of which the read path can distinguish from a good
    one. Both spellings are covered because an absolute path silently discards the
    root it is joined to, which is not obvious from reading the join.
    """
    for payload in ("../../../../etc/passwd", "/etc/passwd"):
        with pytest.raises(BlobError) as caught:
            blobstore.path_for(payload)
        assert caught.value.reason == "path_escape"


def test_an_uppercase_digest_is_folded_rather_than_refused() -> None:
    """A hash pasted from another tool is often uppercase, and that is not an
    attack. Folding it keeps one canonical form on disk."""
    digest = _sha(_pdf(b"case"))
    assert blobstore.validate_sha256(digest.upper()) == digest


def test_a_traversal_in_the_read_route_never_reaches_the_filesystem(
    client: TestClient,
) -> None:
    """Two different layers refuse this, and the test names both so neither is
    mistaken for the other.

    Percent-encoded on purpose: a literal `../` is normalised away by the client
    before the request is even sent, so the obvious spelling of this test passes
    while exercising nothing. With the slashes encoded the payload survives to the
    router — which then cannot match `/{sha256}` against a path containing separators
    and answers 404. Only a *slash-free* payload gets as far as the handler, and
    there `validate_sha256` refuses it with our own reason.
    """
    escaped_slashes = client.get("/api/documents/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    assert escaped_slashes.status_code == 404, escaped_slashes.text

    reaches_handler = client.get("/api/documents/%2e%2e")
    assert reaches_handler.status_code == 422, reaches_handler.text
    assert reaches_handler.json()["detail"]["reason"] == "invalid_sha256"


def test_a_traversal_in_a_claimed_hash_is_refused(client: TestClient) -> None:
    """Exactly 64 characters, so it clears the parameter's own length bound and the
    only thing left refusing it is `validate_sha256` — which is the defence that
    actually stands in front of the filesystem."""
    response = _upload(client, _pdf(b"claim"), sha256="../" * 21 + "x")
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_sha256"


def test_an_over_long_claimed_hash_never_reaches_the_handler(client: TestClient) -> None:
    """The `max_length` bound on the query parameter refuses it first, as a plain
    FastAPI validation error. Asserted so the two layers stay distinguishable: this
    one is the schema's, the one above is ours."""
    response = _upload(client, _pdf(b"claim-long"), sha256="../" * 40 + "etc/passwd")
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


# ---------------------------------------------------------------------------
# Rows: store_document
# ---------------------------------------------------------------------------


def test_two_parts_sharing_a_datasheet_store_one_blob(db: Session) -> None:
    """The common case for a family, and the reason the store is content-addressed
    at all: `document_links` is M:N precisely so this is one file."""
    data = _pdf(b"GRM188 family")
    first = documents.store_document(db, data=data, media_type=PDF, kind=DocumentKind.DATASHEET)
    second = documents.store_document(db, data=data, media_type=PDF, kind=DocumentKind.DATASHEET)

    assert first.created is True
    assert second.created is False
    assert second.deduplicated is True
    assert second.document.id == first.document.id
    assert db.execute(select(func.count()).select_from(Document)).scalar_one() == 1

    for name in ("GRM188R71C104KA01", "GRM188R71C105KA12"):
        part = make_part(db, name=name)
        documents.attach(
            db, document=first.document, entity_type=EntityType.PART, entity_pk=part.id
        )
    assert db.execute(select(func.count()).select_from(DocumentLink)).scalar_one() == 2
    _assert_one_primary_per_role(db)


def test_a_re_upload_does_not_rewrite_the_existing_row(db: Session) -> None:
    """A strict no-op, so "the second upload changed nothing" stays testable. A
    silent merge would let a wrong `kind` on one re-upload rewrite a right one."""
    data = _pdf(b"norewrite")
    first = documents.store_document(
        db,
        data=data,
        media_type=PDF,
        kind=DocumentKind.DATASHEET,
        source_url="https://example.invalid/ds.pdf",
        original_filename="original.pdf",
    )
    second = documents.store_document(
        db,
        data=data,
        media_type=PDF,
        kind=DocumentKind.APP_NOTE,
        source_url="https://elsewhere.invalid/other.pdf",
        original_filename="renamed.pdf",
    )
    assert second.document.kind == DocumentKind.DATASHEET
    assert second.document.source_url == "https://example.invalid/ds.pdf"
    assert second.document.original_filename == "original.pdf"
    assert first.document.id == second.document.id


def test_a_claimed_hash_that_does_not_match_the_bytes_is_refused(db: Session) -> None:
    """A truncated upload still hashes to *something* valid, so without this the
    only detectable difference between a truncation and a different document is
    one the client already knew and could not say."""
    with pytest.raises(BlobError) as caught:
        documents.store_document(
            db,
            data=_pdf(b"real"),
            media_type=PDF,
            kind=DocumentKind.DATASHEET,
            claimed_sha256=_sha(_pdf(b"different")),
        )
    assert caught.value.reason == "hash_mismatch"


def test_a_correct_claimed_hash_is_accepted(db: Session) -> None:
    data = _pdf(b"claimed")
    stored = documents.store_document(
        db, data=data, media_type=PDF, kind=DocumentKind.DATASHEET, claimed_sha256=_sha(data)
    )
    assert stored.document.sha256 == _sha(data)


def test_an_orphan_blob_is_adopted_rather_than_duplicated(db: Session) -> None:
    """The recovery path the blob-before-row write order buys.

    An upload interrupted between the disk write and the commit leaves a blob with
    no row. The next attempt must adopt it — `created` **and** `deduplicated` both
    true — because the alternative is a row that never gets written for bytes that
    are already there.
    """
    data = _pdf(b"orphan")
    blobstore.store(data, media_type=PDF)  # the blob, with no row: the crash state

    stored = documents.store_document(db, data=data, media_type=PDF, kind=DocumentKind.DATASHEET)
    assert stored.created is True
    assert stored.deduplicated is True


def test_page_count_is_null_because_the_api_never_opens_the_file(db: Session) -> None:
    """ADR 0005. A document with no page count is a first-class state: the PDF is
    stored, served and attached, and only the text extraction waits."""
    stored = documents.store_document(
        db, data=_pdf(b"nopages"), media_type=PDF, kind=DocumentKind.DATASHEET
    )
    assert stored.document.page_count is None


# ---------------------------------------------------------------------------
# The primary rule
# ---------------------------------------------------------------------------


def _store(db: Session, body: bytes) -> Document:
    return documents.store_document(
        db, data=_pdf(body), media_type=PDF, kind=DocumentKind.DATASHEET
    ).document


def test_attaching_a_second_primary_demotes_the_first(db: Session) -> None:
    part = make_part(db, name="Part with two sheets")
    old = _store(db, b"rev-a")
    new = _store(db, b"rev-b")

    documents.attach(db, document=old, entity_type=EntityType.PART, entity_pk=part.id)
    documents.attach(db, document=new, entity_type=EntityType.PART, entity_pk=part.id)

    primary = documents.primary_link(db, entity_type=EntityType.PART, entity_pk=part.id)
    assert primary is not None
    assert primary.document_id == new.id
    _assert_one_primary_per_role(db)


def test_the_first_link_in_a_role_is_primary_even_when_not_asked_for(db: Session) -> None:
    """Otherwise a part can hold a datasheet whose `/datasheet` route 404s — the
    invariant's *at least one* half, which no index can enforce."""
    part = make_part(db, name="Reluctant primary")
    document = _store(db, b"only-one")
    link, created = documents.attach(
        db,
        document=document,
        entity_type=EntityType.PART,
        entity_pk=part.id,
        is_primary=False,
    )
    assert created is True
    assert link.is_primary is True
    _assert_one_primary_per_role(db)


def test_a_non_primary_attach_alongside_an_existing_primary_is_respected(db: Session) -> None:
    part = make_part(db, name="Two sheets, one chosen")
    chosen = _store(db, b"chosen")
    other = _store(db, b"other")

    documents.attach(db, document=chosen, entity_type=EntityType.PART, entity_pk=part.id)
    link, _ = documents.attach(
        db,
        document=other,
        entity_type=EntityType.PART,
        entity_pk=part.id,
        is_primary=False,
    )
    assert link.is_primary is False
    primary = documents.primary_link(db, entity_type=EntityType.PART, entity_pk=part.id)
    assert primary is not None and primary.document_id == chosen.id
    _assert_one_primary_per_role(db)


def test_roles_have_independent_primaries(db: Session) -> None:
    """Attaching a photo must not demote the datasheet. "One primary per part"
    would do exactly that, which is why the rule is per (part, role)."""
    part = make_part(db, name="Sheet and photo")
    sheet = _store(db, b"sheet")
    photo = _store(db, b"photo")

    documents.attach(
        db,
        document=sheet,
        entity_type=EntityType.PART,
        entity_pk=part.id,
        role=DocumentRole.DATASHEET,
    )
    documents.attach(
        db,
        document=photo,
        entity_type=EntityType.PART,
        entity_pk=part.id,
        role=DocumentRole.PHOTO,
    )

    datasheet = documents.primary_link(
        db, entity_type=EntityType.PART, entity_pk=part.id, role=DocumentRole.DATASHEET
    )
    assert datasheet is not None and datasheet.document_id == sheet.id
    _assert_one_primary_per_role(db)


def test_re_attaching_the_same_document_promotes_instead_of_duplicating(db: Session) -> None:
    part = make_part(db, name="Promote by re-attach")
    first = _store(db, b"first")
    second = _store(db, b"second")

    link_a, created_a = documents.attach(
        db, document=first, entity_type=EntityType.PART, entity_pk=part.id
    )
    documents.attach(db, document=second, entity_type=EntityType.PART, entity_pk=part.id)
    link_b, created_b = documents.attach(
        db, document=first, entity_type=EntityType.PART, entity_pk=part.id
    )

    assert created_a is True
    assert created_b is False
    assert link_b.id == link_a.id
    assert db.execute(select(func.count()).select_from(DocumentLink)).scalar_one() == 2
    _assert_one_primary_per_role(db)


def test_detaching_the_primary_promotes_the_oldest_survivor(db: Session) -> None:
    """The *at least one* half of the invariant, and the only way it breaks in
    practice: three sheets, delete the chosen one, and without promotion the part
    has three datasheets and no primary datasheet."""
    part = make_part(db, name="Three sheets")
    sheets = [_store(db, f"sheet-{index}".encode()) for index in range(3)]
    for sheet in sheets:
        documents.attach(
            db, document=sheet, entity_type=EntityType.PART, entity_pk=part.id, is_primary=False
        )
    # The last attach with is_primary=False left the first one primary.
    documents.attach(db, document=sheets[2], entity_type=EntityType.PART, entity_pk=part.id)

    detachment = documents.detach(
        db, document=sheets[2], entity_type=EntityType.PART, entity_pk=part.id
    )
    assert detachment.removed == 1
    assert len(detachment.promoted) == 1
    assert detachment.promoted[0].document_id == sheets[0].id
    _assert_one_primary_per_role(db)


def test_detaching_the_last_link_leaves_no_primary_and_that_is_correct(db: Session) -> None:
    part = make_part(db, name="Sole sheet")
    sheet = _store(db, b"sole")
    documents.attach(db, document=sheet, entity_type=EntityType.PART, entity_pk=part.id)

    detachment = documents.detach(
        db, document=sheet, entity_type=EntityType.PART, entity_pk=part.id
    )
    assert detachment.removed == 1
    assert detachment.promoted == []
    assert documents.primary_link(db, entity_type=EntityType.PART, entity_pk=part.id) is None
    _assert_one_primary_per_role(db)


def test_detaching_does_not_delete_the_document_or_the_blob(db: Session) -> None:
    """One sheet serves a family, so a part dropping its link says nothing about
    whether the file is still wanted."""
    part = make_part(db, name="Detach keeps the file")
    sheet = _store(db, b"kept")
    documents.attach(db, document=sheet, entity_type=EntityType.PART, entity_pk=part.id)
    documents.detach(db, document=sheet, entity_type=EntityType.PART, entity_pk=part.id)

    assert db.get(Document, sheet.id) is not None
    assert blobstore.exists(sheet.sha256, ".pdf") is True


def test_a_document_attached_to_two_entity_types_keeps_separate_primaries(db: Session) -> None:
    """Phase 3's tray images attach to a lot, Phase 5's datasheets to a part, and
    the polymorphic link is what stops that being a migration per phase."""
    part = make_part(db, name="Polymorphic")
    image = _store(db, b"tray")
    documents.attach(
        db,
        document=image,
        entity_type=EntityType.PART,
        entity_pk=part.id,
        role=DocumentRole.COUNT_EVIDENCE,
    )
    documents.attach(
        db,
        document=image,
        entity_type=EntityType.STOCK_LOT,
        entity_pk=1,
        role=DocumentRole.COUNT_EVIDENCE,
    )
    _assert_one_primary_per_role(db)
    assert db.execute(select(func.count()).select_from(DocumentLink)).scalar_one() == 2


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_upload_stream_and_redirect_end_to_end(client: TestClient) -> None:
    """The worked path from `docs/PLAN.md`: upload a sheet against a part, then
    reach it from the part in one hop."""
    part = client.post("/api/parts", json={"name": "STM32F103C8T6"}).json()["part"]
    data = _pdf(b"stm32")

    upload = _upload(client, data, kind="datasheet", part_id=part["id"], filename="STM32F103.pdf")
    assert upload.status_code == 200, upload.text
    body = upload.json()
    assert body["created"] is True
    assert body["deduplicated"] is False
    assert body["document"]["sha256"] == _sha(data)
    assert body["document"]["byte_size"] == len(data)
    assert body["document"]["page_count"] is None
    assert body["link"]["is_primary"] is True
    assert body["link"]["role"] == "datasheet"

    stream = client.get(body["document"]["url"])
    assert stream.status_code == 200
    assert stream.content == data
    assert stream.headers["content-type"] == PDF
    # Inline, so the browser's own PDF viewer opens it rather than downloading it.
    assert stream.headers["content-disposition"] == 'inline; filename="STM32F103.pdf"'
    # Cacheable forever, which is only correct because the URL is a content
    # address: these bytes cannot change without the URL changing.
    assert "immutable" in stream.headers["cache-control"]

    redirect = client.get(f"/api/parts/{part['id']}/datasheet", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == f"/api/documents/{_sha(data)}"


def test_the_second_upload_over_http_is_observably_a_no_op(client: TestClient) -> None:
    data = _pdf(b"http-dedup")
    first = _upload(client, data).json()
    second = _upload(client, data).json()

    assert first["created"] is True and first["deduplicated"] is False
    assert second["created"] is False and second["deduplicated"] is True
    assert second["document"]["id"] == first["document"]["id"]


def test_an_html_error_page_uploaded_as_a_pdf_is_refused(client: TestClient) -> None:
    response = _upload(client, b"<!doctype html><h1>404</h1>")
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "content_mismatch"


def test_an_unsupported_media_type_is_a_415(client: TestClient) -> None:
    response = _upload(client, b"MZ\x90\x00", media_type="application/x-msdownload")
    assert response.status_code == 415


def test_uploading_against_an_unknown_part_is_a_404(client: TestClient) -> None:
    response = _upload(client, _pdf(b"nopart"), part_id=999_999)
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_part"


def test_a_part_with_no_datasheet_redirects_nowhere(client: TestClient) -> None:
    """An ordinary state, not an error condition: a part created from a scan in one
    tap has no datasheet, and that is the entire intake design."""
    part = client.post("/api/parts", json={"name": "Stub from a scan"}).json()["part"]
    response = client.get(f"/api/parts/{part['id']}/datasheet", follow_redirects=False)
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "no_datasheet"


def test_attach_and_detach_over_http_maintain_the_primary(client: TestClient) -> None:
    part = client.post("/api/parts", json={"name": "Family member"}).json()["part"]
    first = _upload(client, _pdf(b"rev-1"), part_id=part["id"]).json()
    second = _upload(client, _pdf(b"rev-2")).json()

    attach = client.post(
        f"/api/parts/{part['id']}/documents",
        json={"sha256": second["document"]["sha256"], "role": "datasheet", "is_primary": True},
    )
    assert attach.status_code == 200, attach.text
    assert attach.json()["created"] is True
    assert attach.json()["link"]["is_primary"] is True

    listing = client.get(f"/api/parts/{part['id']}/documents").json()
    assert [link["is_primary"] for link in listing["links"]] == [True, False]
    assert listing["links"][0]["document"]["sha256"] == second["document"]["sha256"]

    redirect = client.get(f"/api/parts/{part['id']}/datasheet", follow_redirects=False)
    assert redirect.headers["location"].endswith(second["document"]["sha256"])

    detach = client.delete(f"/api/parts/{part['id']}/documents/{second['document']['sha256']}")
    assert detach.status_code == 200, detach.text
    assert detach.json()["detached"] == 1
    promoted = detach.json()["promoted"]
    assert len(promoted) == 1
    assert promoted[0]["document"]["sha256"] == first["document"]["sha256"]

    # The redirect follows the promotion, which is the point of promoting at all.
    redirect = client.get(f"/api/parts/{part['id']}/datasheet", follow_redirects=False)
    assert redirect.headers["location"].endswith(first["document"]["sha256"])


def test_detaching_something_that_was_never_attached_is_a_404(client: TestClient) -> None:
    part = client.post("/api/parts", json={"name": "Nothing attached"}).json()["part"]
    document = _upload(client, _pdf(b"unattached")).json()
    response = client.delete(f"/api/parts/{part['id']}/documents/{document['document']['sha256']}")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_link"


def test_attaching_an_unknown_document_is_a_404(client: TestClient) -> None:
    part = client.post("/api/parts", json={"name": "Attach a ghost"}).json()["part"]
    response = client.post(
        f"/api/parts/{part['id']}/documents",
        json={"sha256": "0" * 64, "role": "datasheet"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_document"


def test_a_filename_cannot_inject_a_header(client: TestClient) -> None:
    """`original_filename` is client text that ends up in `Content-Disposition`.
    A CR/LF in it is header injection and a quote breaks the parameter it sits in,
    so the field is allowlisted down to a safe alphabet rather than escaped."""
    response = _upload(
        client,
        _pdf(b"inject"),
        filename='ev"il\r\nX-Injected: yes.pdf',
    )
    assert response.status_code == 200
    sha256 = response.json()["document"]["sha256"]
    stream = client.get(f"/api/documents/{sha256}")
    disposition = stream.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert disposition.count('"') == 2
    assert "X-Injected" not in stream.headers


def test_a_row_whose_blob_is_missing_is_a_server_error_not_a_404(client: TestClient) -> None:
    """The state the blob-before-row write order exists to prevent, so it has to be
    loud: it means the volume lost data, not that the client asked for the wrong
    thing, and a 404 here would read as "no such datasheet" forever."""
    data = _pdf(b"vanishing")
    document = _upload(client, data).json()["document"]
    blobstore.blob_path(document["sha256"], ".pdf").unlink()

    response = client.get(document["url"])
    assert response.status_code == 500
    assert response.json()["detail"]["reason"] == "missing_blob"


def test_reading_a_well_formed_but_unknown_hash_is_a_404(client: TestClient) -> None:
    """64 hex characters clear `validate_sha256` and reach `by_sha256`, which finds
    nothing. Distinguished from the malformed-hash case below: this is "no such
    document", not "not even a valid address"."""
    response = client.get(f"/api/documents/{'0' * 64}")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "unknown_document"


def test_reading_a_malformed_hash_is_a_422_not_a_500(client: TestClient) -> None:
    """A slash-free malformed digest reaches the handler (see the traversal test
    above for the payload that does not) and must be refused as *our* input error,
    not crash trying to build a path from it."""
    response = client.get(f"/api/documents/{'z' * 64}")
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "invalid_sha256"


def test_an_oversized_upload_over_http_is_a_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit-level `test_an_oversized_document_is_refused` proves `blobstore`
    itself refuses; this proves the route maps that refusal to 413 rather than
    accepting the body and only then complaining — an unbounded read is the exact
    way an upload endpoint fills the disk that also holds the database."""
    monkeypatch.setattr(blobstore, "MAX_DOCUMENT_BYTES", 16)
    response = _upload(client, _pdf(b"x" * 64))
    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "document_too_large"


def test_a_range_request_gets_a_genuine_partial_response(client: TestClient) -> None:
    """A phone resuming a multi-megabyte datasheet over flaky wifi sends a `Range`
    header; this route must answer it rather than restart the whole file every
    time. `FileResponse` provides this for free, so the test pins the behaviour
    rather than the implementation — a future rewrite that returns a bare
    `Response(content=...)` instead of `FileResponse` would go red here even
    though the unsliced-content tests above would still pass."""
    data = _pdf(b"range-me" * 500)
    sha256 = _upload(client, data).json()["document"]["sha256"]

    response = client.get(f"/api/documents/{sha256}", headers={"Range": "bytes=10-19"})
    assert response.status_code == 206
    assert response.content == data[10:20]
    assert response.headers["content-range"] == f"bytes 10-19/{len(data)}"
    assert response.headers["accept-ranges"] == "bytes"


def test_the_read_route_streams_the_blob_rather_than_slurping_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read_document` hands `FileResponse` a `Path` and nothing else; the route
    itself never holds the bytes. Proven by breaking the one call that *would*
    slurp the whole file — `Path.read_bytes`, which is what `blobstore.read` uses
    for the scrub-job/verify path but which `read_document` must not reach for —
    and confirming the response still streams through untouched. A regression
    that swapped `FileResponse` for `Response(content=blobstore.read(...))` would
    turn this red without touching any assertion about the bytes themselves."""
    data = _pdf(b"stream-me" * 2000)  # several 64 KiB chunks worth
    sha256 = _upload(client, data).json()["document"]["sha256"]

    def _forbidden(self: Path) -> bytes:
        raise AssertionError("the read route must stream the blob, not read it whole")

    monkeypatch.setattr(Path, "read_bytes", _forbidden)
    response = client.get(f"/api/documents/{sha256}")
    assert response.status_code == 200
    assert response.content == data
