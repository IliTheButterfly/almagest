"""The bytes on disk. No database, no HTTP, no PDF library.

`docs/PLAN.md`: `data/datasheets/{sha256[0:2]}/{sha256[2:4]}/{sha256}.pdf`,
git-style fanout, **hash computed before the write so dedup is free**. That
sentence is the whole design and this module is the whole implementation of it.

Four things here are not obvious, and each of them is a way to lose data quietly.

## The hash comes first, so dedup is a `Path.exists()`

Digest, then look, then maybe write. A store that wrote first and compared after
would have to read the blob back to know whether it had changed anything, and
would spend an fsync per re-upload of a datasheet it already had. Because the
name is a function of the content, "already stored" and "identical" are the same
question — so a second upload of the same PDF **writes nothing at all** and says
so (`StoredBlob.deduplicated`), rather than overwriting a good blob with bytes
that happen to be equal.

With one qualification, added after review: the name is a *claim* about the
content, and dedup used to accept it on `exists()` alone. The blob's length is
free to check — the correct bytes are in memory already — so a stored blob whose
size contradicts the bytes being uploaded is rewritten instead of adopted. See
`store`.

## A partial write must never appear at the final name

Bytes go to a sibling temp file, get `fsync`ed, and are then moved into place
with `Path.replace` (`os.replace` — atomic on POSIX, and the same-directory temp
guarantees the same filesystem, which is what atomicity depends on). A reader
therefore sees the final name either absent or complete: never a truncated blob
sitting at a name that asserts its own sha256, which is the one corruption this
store cannot detect by itself and cannot recover from, because every future
upload of those bytes would dedup onto the damaged copy.

The `fsync` before the move is the part worth defending. Without it the rename
can reach disk while the data has not, and a power cut then leaves a
**zero-length or truncated file at a valid hash** — precisely the state above.
The directory entry is deliberately *not* fsynced: if that is lost the blob is
missing, and a missing blob is loud, detectable and re-fetchable, while a corrupt
one is silent. The asymmetry is the whole reason to spend one fsync and not two.

Verification after the write is offered (`verify`) but not performed on the write
path. Re-reading proves only that the page cache agrees with itself; what it
would catch — later bit-rot — it cannot catch at write time. So it belongs in a
scrub job, which is what `verify` exists for and which is
`app.services.documents.scrub`, reached by `POST /api/system/blobs/scrub` and run
by `python -m app.scripts.maintenance --scrub`. It went without a caller of any
kind for a while, and then without a *deployed* one — the function existed and
only a test ever called it, which detects exactly as much bit rot as no scrub at
all. It is deliberately not folded into the nightly cache pass: it reads every
blob in full, so it would set that pass's duration.

## The only value that reaches a path is a validated digest

`validate_sha256` rejects anything that is not exactly 64 hex characters, and it
is the sole door into `blob_path`. A traversal payload fails on the character
class before any `Path` is built. `blob_path` then re-checks containment against
the root, which is unreachable today and stays in anyway: the regex is one
well-meaning edit from accepting something else, and the cost of being wrong is
reading or writing an arbitrary file. The suffix likewise comes from
`MEDIA_TYPES`, never from a client-supplied filename.

## The magic bytes are checked, on a handful of bytes and no dependency

`docs/PLAN.md` notes external datasheet URLs rot within a few years, and a dead
one usually answers with an HTML error page rather than a 404 — served, often
enough, with the content type that was asked for. Stored unchecked, that page
becomes a permanent "datasheet" that opens as a blank tab in the browser's PDF
viewer, and the symptom looks like a broken viewer instead of a dead link. Five
bytes of prefix check turns it into a refusal at intake.

This is not content sniffing and does not pretend to validate a PDF: it is the
cheapest available guard against the byte stream being obviously not the declared
format. Validating the structure would need a parser, and per ADR 0005 the API
does not have one.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.config import get_settings

#: Lowercase hex, exactly 64 symbols. `\A`/`\Z` rather than `^`/`$` because `$`
#: also matches before a trailing newline, so `"<64 hex>\n"` would pass — and a
#: filename with a newline in it is exactly the kind of thing that then breaks
#: something further downstream.
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: The API reads the whole body into memory to hash it, so this bounds memory as
#: well as the volume. Datasheets are 1-10 MB; a 64 MiB ceiling is absurd for one
#: and still nowhere near the ~5 Gi the deployment sizes its PVC at. Not a
#: security boundary — a single-user LAN install has no adversary — but an
#: unbounded upload route is how a mis-aimed script fills the disk that also
#: holds the database.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class MediaType:
    """One accepted format: the suffix its blobs get, and how its bytes start."""

    suffix: str
    #: Any one of these prefixes is accepted. A tuple because JPEG has several
    #: legal fourth bytes and there is no reason to enumerate variants as separate
    #: media types.
    magic: tuple[bytes, ...]


#: Adding a format is one entry. Deliberately short: PDFs are what Phase 4 stores
#: and PNG/JPEG are what Phase 3's counting camera and the marking reader produce
#: (`docs/PLAN.md` puts both in `documents`). Anything not listed is refused
#: rather than stored with a guessed suffix.
MEDIA_TYPES: dict[str, MediaType] = {
    "application/pdf": MediaType(suffix=".pdf", magic=(b"%PDF-",)),
    "image/png": MediaType(suffix=".png", magic=(b"\x89PNG\r\n\x1a\n",)),
    # SOI marker plus the first byte of the following marker, which is 0xE0
    # (JFIF), 0xE1 (Exif) or 0xDB in practice. Checking only `FFD8` would accept
    # a two-byte file; checking the whole JFIF header would reject valid Exif.
    "image/jpeg": MediaType(suffix=".jpg", magic=(b"\xff\xd8\xff",)),
}


class BlobError(ValueError):
    """A document that cannot be stored or addressed as asked.

    Carries a `reason` the route maps to a status code, matching
    `app.services.labels.LabelError` and `app.services.provisioning.
    ProvisioningError`. `app.services.documents` raises this too, so a route has
    one class to catch and one vocabulary to map.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def root() -> Path:
    """The storage root, read from settings **at call time**.

    Never cached in a module constant: `get_settings()` is a process-wide
    singleton that tests mutate in place (see `_isolate_datasheet_dir` in
    `tests/conftest.py`), so a value captured at import would send every test's
    writes into the repo's own `data/datasheets`.

    `Settings.datasheet_dir` is reused for images as well as PDFs. The name is
    `docs/PLAN.md`'s and stays; a second root would mean a second thing to back
    up and a second PVC path for no gain, since a content-addressed tree has no
    per-kind directories to keep apart.
    """
    return get_settings().datasheet_dir


def validate_sha256(raw: str) -> str:
    """Normalise and check a client-supplied digest, or refuse.

    **The only door into `blob_path`.** Uppercase is accepted and folded because
    a hash pasted from another tool is often uppercase and that is a spelling of
    the same digest. Surrounding whitespace is **not** stripped, and that is the
    deliberate half: it is not another spelling, it is a malformed parameter, and
    a store that quietly accepted `"<digest>\\n"` would be one relaxation away from
    accepting whatever else came attached to it.
    """
    candidate = raw.lower()
    if not _SHA256_RE.match(candidate):
        raise BlobError(
            f"not a sha256: {raw!r} (expected 64 hex characters)",
            reason="invalid_sha256",
        )
    return candidate


def canonical_media_type(media_type: str) -> str:
    """The registered spelling of a declared media type, or a refusal.

    **The only value that should ever be stored or served**, and the reason it
    exists as its own function: the type parameters (`; charset=…`) and the case
    are the half of a client-supplied media type that nothing validates, and the
    stored column is handed straight to a response header. A row holding
    `"application/pdf; charset=x\\r\\nX-Injected: yes"` made *its own read route*
    unserviceable — the server's header writer refuses the value, so every GET of
    that document died — and no re-upload could repair it, because re-uploading
    existing bytes is documented to change nothing about the row. Same class of
    bug as an unsanitised `Content-Disposition` filename, one field over.

    Dropping the parameters loses nothing here. None of the three stored formats
    has a parameter that changes what the bytes are — `charset` is meaningless for
    a PDF or a PNG — so the type *is* the registered token, and folding case and
    padding means the column has one spelling per format rather than one per
    client. Consumers that key off it (`document_text.EXTRACTABLE_MEDIA_TYPES`, a
    `switch` in the PWA) then need no folding of their own.
    """
    declared = media_type.split(";", 1)[0].strip().lower()
    if declared not in MEDIA_TYPES:
        supported = ", ".join(sorted(MEDIA_TYPES))
        raise BlobError(
            f"{media_type!r} cannot be stored; supported: {supported}",
            reason="unsupported_media_type",
        )
    return declared


def suffix_for(media_type: str) -> str:
    """The file suffix a media type's blobs get.

    Derived from the declared type, **never** from an uploaded filename: a
    filename is client-controlled text and the suffix ends up in a path.
    """
    return MEDIA_TYPES[canonical_media_type(media_type)].suffix


def relative_path(sha256: str, suffix: str) -> str:
    """`{aa}/{bb}/{sha256}{suffix}` — what `documents.storage_path` holds.

    A string with POSIX separators rather than a `Path`, because it is stored in
    a database column and read back on whatever platform: a Windows-flavoured
    `\\` in a committed row would not resolve on the deployment target.
    """
    digest = validate_sha256(sha256)
    return f"{digest[0:2]}/{digest[2:4]}/{digest}{suffix}"


def blob_path(sha256: str, suffix: str) -> Path:
    """Absolute path of one blob, validated twice over.

    The digest is checked by `validate_sha256` (which is what actually stops a
    traversal) and the assembled path is then checked to be inside the root. The
    second check cannot fail while the first is correct; it is here because it is
    free and because "the digest regex was relaxed" is a plausible future edit
    whose blast radius is arbitrary filesystem access.
    """
    base = root().resolve()
    candidate = (base / relative_path(sha256, suffix)).resolve()
    if not candidate.is_relative_to(base):
        raise BlobError(
            f"refusing a path outside the store: {candidate}",
            reason="path_escape",
        )
    return candidate


def path_for(storage_path: str) -> Path:
    """Resolve a stored `documents.storage_path` back to an absolute path.

    Takes the *recorded* path rather than recomputing it from the digest, so a
    row written under an older fanout rule still resolves. Containment is checked
    for the same reason as in `blob_path` — the column is trusted, but it is
    trusted data reaching a filesystem call, and that combination has a poor
    record.
    """
    base = root().resolve()
    candidate = (base / storage_path).resolve()
    if not candidate.is_relative_to(base):
        raise BlobError(
            f"stored path escapes the store: {storage_path!r}",
            reason="path_escape",
        )
    return candidate


@dataclass(frozen=True)
class StoredBlob:
    """What `store` did, in enough detail for the caller to build a row."""

    sha256: str
    #: Relative, for `documents.storage_path`.
    storage_path: str
    byte_size: int
    #: The **canonical** spelling of the declared type (see
    #: `canonical_media_type`). Reported rather than left for the caller to
    #: re-derive, so the value that decided the suffix on disk is the same one that
    #: reaches the row and the response header — two normalisations of one string
    #: is two chances for them to disagree.
    media_type: str
    #: True when the bytes were already on disk and **nothing was written**. The
    #: caller reports this rather than swallowing it: "we already had this" and
    #: "we just stored this" are different answers to an upload, and a UI that
    #: cannot tell them apart cannot tell a user why a re-upload was instant.
    deduplicated: bool


def store(data: bytes, *, media_type: str) -> StoredBlob:
    """Hash the bytes, then store them under their own digest if they are new.

    Refusals, all before anything touches the filesystem: an empty body (the
    sha256 of nothing is a perfectly valid address, and would produce a
    legitimate-looking document containing no document), a body over
    `MAX_DOCUMENT_BYTES`, an unsupported media type, and bytes whose prefix
    contradicts the declared type.
    """
    if not data:
        raise BlobError("refusing to store an empty document", reason="empty_document")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise BlobError(
            f"{len(data)} bytes exceeds the {MAX_DOCUMENT_BYTES}-byte limit",
            reason="document_too_large",
        )

    declared = canonical_media_type(media_type)
    suffix = MEDIA_TYPES[declared].suffix
    _check_magic(data, declared)

    digest = hashlib.sha256(data).hexdigest()
    path = blob_path(digest, suffix)
    storage_path = relative_path(digest, suffix)

    if path.exists() and path.stat().st_size == len(data):
        # Free dedup, and a genuine no-op: the file is not opened, not touched,
        # not restated. Its mtime is part of the test for this.
        return StoredBlob(
            sha256=digest,
            storage_path=storage_path,
            byte_size=len(data),
            deduplicated=True,
            media_type=declared,
        )

    # The name is a claim about the content, and the length is the one part of that
    # claim checkable for free — the correct bytes and their size are already in
    # memory, so a stored blob of a different size **provably is not them**. Left
    # alone it would be adopted by this upload, served as authoritative with
    # `immutable`, and never repaired, since a blob is only written when it is
    # absent: the one upload that could fix it is the one that dedups onto it.
    #
    # Rewriting is safe because `_write_atomically` replaces by rename, so a reader
    # holding the old path sees complete bytes either way. This does **not** replace
    # `verify` — an equal-length corruption still needs the scrub
    # (`app.services.documents.scrub`) — it catches the truncated write and the
    # half-restored file, which are the corruptions that actually happen.
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomically(path, data)
    return StoredBlob(
        sha256=digest,
        storage_path=storage_path,
        byte_size=len(data),
        deduplicated=False,
        media_type=declared,
    )


def exists(sha256: str, suffix: str) -> bool:
    return blob_path(sha256, suffix).is_file()


def read(sha256: str, suffix: str) -> bytes:
    path = blob_path(sha256, suffix)
    if not path.is_file():
        raise BlobError(f"no blob stored for {sha256}", reason="missing_blob")
    return path.read_bytes()


def verify(sha256: str, suffix: str) -> bool:
    """Re-hash a stored blob and compare it to its own name.

    Not called on the write path — see the module docstring. It is the check a
    scrub job runs, and the check a test runs to prove the write path stored what
    it claimed.
    """
    return verify_stored(blob_path(sha256, suffix), sha256)


def verify_stored(path: Path, sha256: str) -> bool:
    """Re-hash the blob at an already-resolved path. False when it is absent.

    Split out from `verify` so `app.services.documents.scrub` can check a row's
    **recorded** `storage_path` rather than re-derive one from the digest: a row
    written under an older fanout rule has to be checked where it actually lives,
    or a layout change would make the scrub report the whole store corrupt.
    """
    if not path.is_file():
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == validate_sha256(sha256)


def _check_magic(data: bytes, declared: str) -> None:
    """Takes an already-canonical type: the caller has to have resolved a suffix
    from it anyway, and normalising twice is two places for the two decisions to
    come apart."""
    entry = MEDIA_TYPES[declared]
    if not any(data.startswith(prefix) for prefix in entry.magic):
        raise BlobError(
            f"bytes do not begin like {declared} "
            f"(got {data[:8]!r}) — a fetched error page rather than a document?",
            reason="content_mismatch",
        )


def _write_atomically(path: Path, data: bytes) -> None:
    """Write `data` so that `path` is only ever absent or complete.

    The temp file is a **sibling**, which is what makes the move a rename inside
    one filesystem rather than a copy. A crash between the write and the move
    leaves a `.part` orphan: garbage, safely deletable, and self-healing in the
    sense that the next upload of the same bytes writes the real blob.
    """
    temp = path.with_name(f"{path.name}.{uuid4().hex}.part")
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            # Durability before visibility. Skipping this admits a truncated file
            # at a name that asserts its own hash — see the module docstring.
            # `os.fsync` has no pathlib equivalent: it takes a file descriptor.
            os.fsync(handle.fileno())
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
