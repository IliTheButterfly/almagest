"""Invariants the ORM enforces, so nobody has to remember them.

## Releasing a container's identity

`locations.id` is an `INTEGER PRIMARY KEY` with **no `AUTOINCREMENT`**, so SQLite
reuses a freed rowid. Four tables address a location by that integer —
`object_ids`, `label_prints`, `document_links`, `barcode_aliases` — and none of
them can carry a foreign key, because all four are *polymorphic*: the same
columns point at parts and lots too, and SQL has no FK that means "this pk, in
whichever table that other column names".

So a row left behind after a delete does not merely dangle. It is **adopted by
whatever container is created next**, and a printed card in somebody's hand
silently starts meaning a different drawer. `removal.py` has always said why that
is the worst outcome available here: *a scan that lands on the wrong container is
worse than a scan that lands on nothing.*

**This is a listener rather than a helper three call sites remember to call,
because three call sites did not.** `removal._delete` released them. The
re-layout path did not, until a review found it — a card printed for one cell
resolved to a different cell after a merge and a re-split. Deleting a project's
unreferenced staging boxes did not either, until the next review found *that* —
same symptom, third path. A fourth is a matter of time: nothing about writing
`session.delete(location)` suggests there is an obligation attached to it.

`before_delete` fires for every ORM delete of a `Location`, which is how all
three paths remove one. A bulk `delete(Location).where(...)` would bypass it — no
such statement exists in `app/`, and `tests/unit/test_identity_release.py` says
so out loud rather than leaving it as an assumption.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Delete, delete, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper

from app.models.documents import DocumentLink
from app.models.enums import EntityType
from app.models.identity import ObjectId
from app.models.layout_authoring import LabelPrint
from app.models.scanning import BarcodeAlias
from app.models.storage import Location


def identity_release_statements(location_id: int) -> tuple[Delete, ...]:
    """The four deletes that give up every name pointing at `location_id`.

    * `object_ids` — the short id. Left behind, `/api/resolve` reports the code
      as `resolved` and hands back a pk that either does not exist or belongs to
      somebody else.
    * `label_prints` — what was printed and when, which is how "is this card
      current?" is answerable at all.
    * `document_links` — a photograph, which after rowid reuse becomes the *next*
      container's picture.
    * `barcode_aliases` — a code somebody taught to mean this drawer.
    """
    return (
        delete(ObjectId).where(
            ObjectId.entity_type == EntityType.LOCATION, ObjectId.entity_pk == location_id
        ),
        delete(LabelPrint).where(
            LabelPrint.entity_type == EntityType.LOCATION, LabelPrint.entity_pk == location_id
        ),
        delete(DocumentLink).where(
            DocumentLink.entity_type == EntityType.LOCATION,
            DocumentLink.entity_pk == location_id,
        ),
        delete(BarcodeAlias).where(
            BarcodeAlias.entity_type == EntityType.LOCATION,
            BarcodeAlias.entity_pk == location_id,
        ),
    )


@event.listens_for(Location, "before_delete")
def _release_identity_on_delete(
    _mapper: Mapper[Any], connection: Connection, target: Location
) -> None:
    """Every ORM delete of a container gives up its names, whoever wrote it.

    On the `connection` rather than through the `Session`: this runs inside the
    flush that is already deleting the row, and issuing new ORM work there is how
    a flush ends up re-entrant. The statements are Core deletes for the same
    reason.
    """
    for statement in identity_release_statements(target.id):
        connection.execute(statement)
