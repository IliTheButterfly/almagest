"""`barcode_aliases`: reading the bindings, and teaching new ones.

This is the whole of the alias-learning loop. A payload nobody can parse comes
back from `/api/scan/resolve` with `suggest_bind`, the user says what it is once,
and a row here makes it resolve at step 2 of the chain from then on.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import AliasKind, EntityType
from app.models.scanning import BarcodeAlias
from app.models.types import utcnow

#: `barcode_aliases.code_norm` is `String(512)`. SQLite ignores the width, so the
#: limit is enforced here instead — quietly storing a 4 kB "code" would work
#: until the first Postgres port and is meaningless besides: no barcode carries
#: that, and a payload that long is a reader fault, not a label.
CODE_NORM_MAX_LENGTH = 512


def lookup(session: Session, code_norm: str) -> list[BarcodeAlias]:
    """Every alias bound to this normalised code, best-ranked first.

    **Symbology is deliberately not part of the lookup**, even though it is part
    of the uniqueness constraint. The same physical label read by a phone camera
    and by a HID wedge arrives with two different symbology spellings, and a
    binding that only worked with the reader that taught it would be a binding
    the user has to teach twice. Symbology is provenance here, not a key.

    Ordered by `hit_count` so that when one code legitimately names several
    things — two suppliers shipping the same EAN — the candidate list leads with
    the one this user actually keeps meaning.
    """
    if not code_norm:
        # An empty key would match nothing, but it can only arrive from a
        # payload that was all separators; short-circuiting says so.
        return []
    return list(
        session.execute(
            select(BarcodeAlias)
            .where(BarcodeAlias.code_norm == code_norm)
            .order_by(BarcodeAlias.hit_count.desc(), BarcodeAlias.id)
        ).scalars()
    )


def record_hit(session: Session, alias_id: int) -> None:
    """Count one unambiguous resolution through `alias_id`.

    Only unambiguous ones. Bumping every candidate of an ambiguous match would
    raise them all equally and flatten the very ordering that exists to break
    that tie, so an ambiguous scan deliberately counts as nothing — the count
    then means "times this binding was the answer", which is what ranking and
    retiring stale bindings both need it to mean.
    """
    alias = session.get(BarcodeAlias, alias_id)
    if alias is None:
        return
    alias.hit_count += 1
    alias.last_hit_at = utcnow()


def upsert(
    session: Session,
    *,
    code_norm: str,
    symbology: str,
    entity_type: EntityType | str,
    entity_pk: int,
    alias_kind: AliasKind | str = AliasKind.WHOLE_PAYLOAD,
    parsed_json: str | None = None,
    hint_qty_milli: int | None = None,
    hint_batch: str | None = None,
) -> tuple[BarcodeAlias, bool]:
    """Teach a binding. Returns `(alias, created)`.

    `UNIQUE(code_norm, symbology, entity_type, entity_pk)` makes re-teaching the
    same answer an update rather than an error, and it counts as a hit: a user
    who binds the same code to the same part a second time is confirming it,
    which is exactly the signal `hit_count` should carry.

    Hints are refreshed rather than accumulated. `hint_qty_milli` and
    `hint_batch` describe the label the binding was last taught from, and a reel
    label reprinted with a new quantity should pre-fill the new one — they are
    hints for the intake form, never authority. The ledger records what the human
    confirms.
    """
    existing = session.execute(
        select(BarcodeAlias).where(
            BarcodeAlias.code_norm == code_norm,
            BarcodeAlias.symbology == symbology,
            BarcodeAlias.entity_type == str(entity_type),
            BarcodeAlias.entity_pk == entity_pk,
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.alias_kind = str(alias_kind)
        if parsed_json is not None:
            existing.parsed_json = parsed_json
        if hint_qty_milli is not None:
            existing.hint_qty_milli = hint_qty_milli
        if hint_batch is not None:
            existing.hint_batch = hint_batch
        existing.hit_count += 1
        existing.last_hit_at = utcnow()
        return existing, False

    alias = BarcodeAlias(
        code_norm=code_norm,
        symbology=symbology,
        entity_type=str(entity_type),
        entity_pk=entity_pk,
        alias_kind=str(alias_kind),
        parsed_json=parsed_json,
        hint_qty_milli=hint_qty_milli,
        hint_batch=hint_batch,
    )
    session.add(alias)
    session.flush()
    return alias, True
