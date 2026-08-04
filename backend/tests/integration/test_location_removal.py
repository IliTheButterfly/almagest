"""Removing a container — Iliana: "I wasn't able to remove items in the workshop."

There was no delete for a location anywhere, and adding one is the delicate case:
`stock_lots.location_id` and `stock_ledger.{from,to}_location_id` are `RESTRICT`
against tables nothing ever deletes from, so a drawer that has *ever* held
anything cannot be deleted at all — ever, by any code path. `app.services.removal`
therefore does one of three things per node, and these tests pin all three plus
the properties that make them safe:

1. **Nothing names it → deleted.** The empty template cell, which is the case that
   has to be ordinary and instant.
2. **History names it → retired.** The row and every ledger entry mentioning it
   stay; the container leaves the tree, its parent's slot canvas, the room plan
   and auto-assignment. Reversible.
3. **Stock is inside → refused, naming what is inside.** At this node or anywhere
   below it. A refusal that does not say what is in the drawer is useless, and
   relocating it silently is worse than an error.

Plus the two the design turns on: a subtree is never recursed into silently, and a
**tag on a removed drawer still resolves and says so** rather than reporting a
blank tag the UI offers to provision.

Real Alembic migrations throughout (`tests/conftest.py`), so the `RESTRICT`
constraints and the ledger's append-only triggers are all really there.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.documents import Document, DocumentLink
from app.models.enums import DocumentKind, DocumentRole, EntityType, LedgerKind
from app.models.identity import ObjectId
from app.models.scanning import BarcodeAlias
from app.models.stock import StockLedger
from app.models.storage import Location, LocationTag
from app.services import shortid
from app.services.tree import location_tree
from tests.factories import make_container_type, make_location, make_lot, make_part, post


def _tree(db: Session, *names: str) -> list[Location]:
    """A chain of locations, parent to child, with the path cache rebuilt."""
    rows: list[Location] = []
    parent_id: int | None = None
    for name in names:
        row = make_location(db, name=name, parent_id=parent_id)
        rows.append(row)
        parent_id = row.id
    location_tree(db).rebuild_paths()
    db.commit()
    return rows


# ---------------------------------------------------------------------------
# 1. The common case: an empty cell just goes
# ---------------------------------------------------------------------------


def test_an_empty_container_is_deleted_outright(client: TestClient, db: Session) -> None:
    """The case Iliana actually hit — a cell stamped out of a template that
    nothing has ever touched. It must be a plain delete, not a tombstone: a
    workshop full of retired mistakes is its own problem."""
    (bin_,) = _tree(db, "Spare drawer")

    preview = client.get(f"/api/locations/{bin_.id}/removal")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["removable"] is True
    assert [node["action"] for node in body["nodes"]] == ["delete"]
    assert body["nodes"][0]["pins"] == []

    removed = client.delete(f"/api/locations/{bin_.id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted_location_ids"] == [bin_.id]
    assert removed.json()["retired_location_ids"] == []

    assert client.get(f"/api/locations/{bin_.id}").status_code == 404
    # `expunge_all`, not `expire_all`: the route's own session deleted the row, so
    # this session's identity map still holds an instance whose row is gone, and
    # refreshing it raises rather than returning None.
    db.expunge_all()
    assert db.get(Location, bin_.id) is None


def test_removing_a_slot_from_a_layout_is_ordinary(client: TestClient, db: Session) -> None:
    """Removing an empty generated slot is not a dangerous operation.

    Asserted as the *cheap path*: the slot leaves its parent's canvas and the
    parent stops counting it, with no confirmation about history to read and
    nothing left behind for a later layout edit to trip over — the partial unique
    index on `(parent_id, slot_label)` means a leftover row holding `B3` would
    block ever laying out a `B3` there again.
    """
    cabinet, _ = _tree(db, "Cabinet", "placeholder")
    slot = make_location(db, name="B3", parent_id=cabinet.id, slot_label="B3", row_idx=1, col_idx=2)
    location_tree(db).rebuild_paths()
    db.commit()

    slot_id, cabinet_id = slot.id, cabinet.id
    assert client.delete(f"/api/locations/{slot_id}").status_code == 200
    db.expunge_all()

    layout = client.get(f"/api/locations/{cabinet_id}/layout").json()
    assert [entry["slot_label"] for entry in layout["slots"]] == []

    # And the label is free again, which is the fact a retired row keeping its
    # `slot_label` would have silently taken away: the partial unique index on
    # `(parent_id, slot_label)` would refuse this insert.
    again = make_location(
        db, name="B3", parent_id=cabinet_id, slot_label="B3", row_idx=1, col_idx=2
    )
    db.flush()
    assert again.slot_label == "B3"


# ---------------------------------------------------------------------------
# 2. Stock inside: refused, and the refusal says what is inside
# ---------------------------------------------------------------------------


def test_stock_inside_refuses_and_names_the_contents(client: TestClient, db: Session) -> None:
    """The refusal has to be actionable. "constraint failed" is not."""
    (bin_,) = _tree(db, "Resistors")
    part = make_part(db, name="Chip resistor", mpn="RC0603FR-071KL")
    make_lot(db, part, bin_, qty_milli=4_200_000)
    db.commit()

    refused = client.delete(f"/api/locations/{bin_.id}")
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["reason"] == "holds_stock"
    assert "RC0603FR-071KL" in detail["message"] or any(
        "RC0603FR-071KL" in blocker["detail"] for blocker in detail["blockers"]
    )
    # The quantity, not just the part: "something is in here" is not the answer.
    assert any("4200" in blocker["detail"] for blocker in detail["blockers"])

    db.expire_all()
    assert db.get(Location, bin_.id) is not None

    # The preview says the same thing without a 409, because nothing was tried.
    preview = client.get(f"/api/locations/{bin_.id}/removal").json()
    assert preview["removable"] is False
    assert preview["blockers"][0]["reason"] == "holds_stock"
    assert "RC0603FR-071KL" in preview["blockers"][0]["detail"]


def test_stock_in_a_descendant_blocks_the_cabinet_and_names_the_drawer(
    client: TestClient, db: Session
) -> None:
    """Deleting the cabinet must not be a way around the drawer's refusal, and
    the message has to say *which* drawer — a cabinet's own emptiness is not the
    question being asked."""
    cabinet, drawer = _tree(db, "Cabinet A", "Drawer 3")
    part = make_part(db, name="Ceramic cap", mpn="C0603C104K")
    make_lot(db, part, drawer, qty_milli=470_000)
    db.commit()

    refused = client.delete(f"/api/locations/{cabinet.id}?recursive=true")
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["reason"] == "holds_stock"
    assert detail["blockers"][0]["location_id"] == drawer.id
    assert "Drawer 3" in detail["blockers"][0]["label_path"]
    assert "C0603C104K" in detail["blockers"][0]["detail"]


def test_an_emptied_lot_does_not_block_but_does_keep_the_row(
    client: TestClient, db: Session
) -> None:
    """A drawn-down lot is the difference between "blocked" and "retired".

    Nothing ever deletes a `stock_lots` row, so a bin that has been emptied still
    has one pointing at it — which cannot block the removal (the bin is empty; the
    user is right) and cannot be deleted either (`RESTRICT`). That is exactly what
    retirement is for, and it is the case a naive implementation gets a 500 on.
    """
    (bin_,) = _tree(db, "Was full once")
    part = make_part(db, name="Diode", mpn="1N4148")
    make_lot(db, part, bin_, qty_milli=0)
    db.commit()

    removed = client.delete(f"/api/locations/{bin_.id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["deleted_location_ids"] == []
    assert removed.json()["retired_location_ids"] == [bin_.id]
    assert removed.json()["nodes"][0]["pins"] == ["has_lots"]

    db.expire_all()
    kept = db.get(Location, bin_.id)
    assert kept is not None and kept.retired_at is not None


# ---------------------------------------------------------------------------
# 3. History names it: retired, and the history is untouched
# ---------------------------------------------------------------------------


def test_a_used_drawer_is_retired_and_its_ledger_survives(client: TestClient, db: Session) -> None:
    """The load-bearing one. Deleting a location must never delete history, and
    being unable to delete must never mean being unable to remove."""
    (bin_,) = _tree(db, "Old home")
    elsewhere = make_location(db, name="New home")
    location_tree(db).rebuild_paths()
    part = make_part(db, name="Regulator", mpn="LM317")
    lot = make_lot(db, part, elsewhere, qty_milli=10_000)
    post(
        db,
        lot,
        10_000,
        kind=LedgerKind.MOVE,
        from_location_id=bin_.id,
        to_location_id=elsewhere.id,
    )
    db.commit()

    before = db.execute(select(func.count()).select_from(StockLedger)).scalar_one()

    removed = client.delete(f"/api/locations/{bin_.id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["retired_location_ids"] == [bin_.id]
    assert "in_ledger" in removed.json()["nodes"][0]["pins"]

    db.expire_all()
    assert db.execute(select(func.count()).select_from(StockLedger)).scalar_one() == before
    row = db.execute(
        select(StockLedger).where(StockLedger.from_location_id == bin_.id)
    ).scalar_one()
    assert row.from_location_id == bin_.id


def test_a_retired_container_leaves_the_tree_and_can_come_back(
    client: TestClient, db: Session
) -> None:
    """Retired means *out of the tree*, not merely flagged — otherwise "remove"
    means nothing on screen. And the retirement is the one undoable half, so the
    restore is part of the same contract."""
    (bin_,) = _tree(db, "Old home")
    part = make_part(db, name="Regulator", mpn="LM317")
    make_lot(db, part, bin_, qty_milli=0)
    db.commit()

    assert client.delete(f"/api/locations/{bin_.id}").status_code == 200

    ids = [node["id"] for node in client.get("/api/locations/tree").json()["nodes"]]
    assert bin_.id not in ids
    with_retired = client.get("/api/locations/tree?include_retired=true").json()["nodes"]
    assert bin_.id in [node["id"] for node in with_retired]
    assert next(n for n in with_retired if n["id"] == bin_.id)["retired_at"] is not None

    back = client.post(f"/api/locations/{bin_.id}/restore")
    assert back.status_code == 200, back.text
    assert back.json()["restored_location_ids"] == [bin_.id]
    assert bin_.id in [node["id"] for node in client.get("/api/locations/tree").json()["nodes"]]
    assert client.get(f"/api/locations/{bin_.id}").json()["retired_at"] is None

    # Restoring something that is not removed is a refusal, not a silent no-op:
    # a second tap on an undo button must not read as success.
    assert client.post(f"/api/locations/{bin_.id}/restore").status_code == 409


def test_a_retired_container_is_never_auto_assigned(client: TestClient, db: Session) -> None:
    """Auto-assignment must not propose putting stock into something the user has
    taken out of the tree. Excluded before scoring, so no rung of the escalation
    ladder — including the one that never refuses — can reach it."""
    _shelf, drawer = _tree(db, "Removed shelf", "Removed drawer")
    part = make_part(db, name="Regulator", mpn="LM317")
    make_lot(db, part, drawer, qty_milli=0)
    db.commit()

    assert client.delete(f"/api/locations/{drawer.id}").status_code == 200

    suggested = client.post(
        "/api/locations/suggest", json={"part_id": part.id, "client_op_id": "sug-1"}
    )
    assert suggested.status_code == 200, suggested.text
    body = suggested.json()
    assert body["location_id"] != drawer.id
    assert drawer.id not in [c["location_id"] for c in body["candidates"]]


# ---------------------------------------------------------------------------
# 4. A subtree is an explicit decision
# ---------------------------------------------------------------------------


def test_a_cabinet_with_drawers_is_refused_until_recursion_is_asked_for(
    client: TestClient, db: Session
) -> None:
    """Silent recursion is unacceptable; so is an error that just says
    "constraint failed". The refusal names the drawers."""
    cabinet, drawer = _tree(db, "Cabinet B", "Drawer 1")

    refused = client.delete(f"/api/locations/{cabinet.id}")
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert detail["reason"] == "has_children"
    assert "Drawer 1" in detail["message"]
    assert [b["location_id"] for b in detail["blockers"]] == [drawer.id]

    db.expire_all()
    assert db.get(Location, cabinet.id) is not None

    cabinet_id, drawer_id = cabinet.id, drawer.id
    recursive = client.delete(f"/api/locations/{cabinet.id}?recursive=true")
    assert recursive.status_code == 200, recursive.text
    assert set(recursive.json()["deleted_location_ids"]) == {cabinet_id, drawer_id}
    db.expunge_all()
    assert db.get(Location, cabinet_id) is None
    assert db.get(Location, drawer_id) is None


def test_a_kept_child_keeps_its_parent(client: TestClient, db: Session) -> None:
    """`locations.parent_id` is `RESTRICT`, so deleting the parent of a retired
    child fails at commit and rolls the whole request back — deepest-first
    ordering does not express that on its own. The parent has to be retired too,
    which is also the honest answer: the cabinet is still holding something."""
    cabinet, drawer = _tree(db, "Cabinet C", "Drawer 9")
    part = make_part(db, name="Diode", mpn="1N4148")
    make_lot(db, part, drawer, qty_milli=0)
    db.commit()

    removed = client.delete(f"/api/locations/{cabinet.id}?recursive=true")
    assert removed.status_code == 200, removed.text
    body = removed.json()
    assert body["deleted_location_ids"] == []
    assert set(body["retired_location_ids"]) == {cabinet.id, drawer.id}
    parent_node = next(n for n in body["nodes"] if n["location_id"] == cabinet.id)
    assert "pinned_by_child" in parent_node["pins"]

    db.expire_all()
    assert db.get(Location, cabinet.id) is not None
    assert db.get(Location, drawer.id) is not None


def test_restoring_a_child_of_a_retired_parent_is_refused(client: TestClient, db: Session) -> None:
    """It would come back invisible, inside something invisible."""
    cabinet, drawer = _tree(db, "Cabinet D", "Drawer 4")
    part = make_part(db, name="Diode", mpn="1N4148")
    make_lot(db, part, drawer, qty_milli=0)
    db.commit()
    assert client.delete(f"/api/locations/{cabinet.id}?recursive=true").status_code == 200

    refused = client.post(f"/api/locations/{drawer.id}/restore")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "ancestor_retired"

    # Restoring the cabinet brings the whole retired subtree back with it.
    back = client.post(f"/api/locations/{cabinet.id}/restore")
    assert back.status_code == 200, back.text
    assert set(back.json()["restored_location_ids"]) == {cabinet.id, drawer.id}


# ---------------------------------------------------------------------------
# 5. The tag in the workshop
# ---------------------------------------------------------------------------


def test_a_tag_on_a_removed_drawer_resolves_and_says_it_is_gone(
    client: TestClient, db: Session
) -> None:
    """The requirement that shapes the whole design.

    An NFC tag is stuck to that drawer with `/s/{short_id}` written on it, and it
    is still in the workshop. It must resolve to "this is gone" — not to a 500,
    and **not to nothing**: a code that resolves to nothing is reported as an
    unknown code and the UI offers to provision it, telling the user the tag is
    blank when it names a drawer that was thrown out. So a tagged location is
    retired rather than deleted, and the binding survives.
    """
    (bin_,) = _tree(db, "Tagged drawer")
    code = shortid.allocate(db, EntityType.LOCATION, bin_.id)
    db.add(
        LocationTag(
            location_id=bin_.id,
            tag_uid="04AABBCCDDEE80",
            ndef_url=f"https://almagest.aether.lan/s/{code}",
            written_at=datetime.now(UTC),
        )
    )
    db.commit()

    removed = client.delete(f"/api/locations/{bin_.id}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["retired_location_ids"] == [bin_.id]
    assert "bound_tag" in removed.json()["nodes"][0]["pins"]

    resolved = client.get(f"/api/resolve/{code}")
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["target"]["retired"] is True
    assert body["target"]["label"] == "Tagged drawer"

    # And the scan resolver agrees, because both go through one describer.
    scanned = client.post("/api/scan/resolve", json={"code": code})
    assert scanned.status_code == 200, scanned.text
    assert scanned.json()["target"]["retired"] is True


def test_a_printed_label_keeps_the_row_but_an_unprinted_code_does_not(
    client: TestClient, db: Session
) -> None:
    """A minted `short_id` is not a physical artifact; a printed one is.

    `instantiate` mints a code for every container root it stamps out, so treating
    a `short_id` as physical would make almost nothing deletable — which is the
    complaint this whole change answers. `last_printed_at` is the fact that a card
    exists in the world, and the `object_ids` row goes with the location when it
    does not, so no code is left resolving to a row that is gone.
    """
    # Siblings, not a chain: a kept child would pin its parent, which is a
    # different rule and has its own test.
    unprinted = make_location(db, name="Never printed")
    printed = make_location(db, name="Printed")
    location_tree(db).rebuild_paths()
    unprinted_code = shortid.allocate(db, EntityType.LOCATION, unprinted.id)
    printed_code = shortid.allocate(db, EntityType.LOCATION, printed.id)
    printed.last_printed_at = datetime.now(UTC)
    db.commit()

    gone = client.delete(f"/api/locations/{unprinted.id}")
    assert gone.status_code == 200, gone.text
    assert gone.json()["deleted_location_ids"] == [unprinted.id]

    kept = client.delete(f"/api/locations/{printed.id}")
    assert kept.status_code == 200, kept.text
    assert kept.json()["retired_location_ids"] == [printed.id]
    assert "printed" in kept.json()["nodes"][0]["pins"]

    db.expunge_all()
    # No orphan: `object_ids` has no foreign key to `locations`, so nothing at the
    # database layer would have stopped the deleted location's code outliving it
    # and resolving to a missing row.
    assert db.get(ObjectId, unprinted_code) is None
    assert client.get(f"/api/resolve/{unprinted_code}").json()["status"] == "unknown"
    # The printed one still resolves, and says it is gone.
    assert client.get(f"/api/resolve/{printed_code}").json()["target"]["retired"] is True


def test_a_deleted_container_takes_its_photo_and_its_taught_codes_with_it(
    client: TestClient, db: Session
) -> None:
    """The two other polymorphic tables, and why an orphan here is not cosmetic.

    `document_links` and `barcode_aliases` both name a location by
    (`entity_type`, `entity_pk`) with no foreign key, exactly as `object_ids`
    does. `locations.id` is a bare `INTEGER PRIMARY KEY` with no `AUTOINCREMENT`
    and `sqlite_sequence` is empty for it, so SQLite hands the freed rowid to the
    **next** container created — which is precisely the loop this whole change
    exists to enable ("I created that by mistake, delete it", then add another).

    So an orphan is not a dangling row nobody reads: it is the deleted drawer's
    photograph presented as the new drawer's own, and a barcode that resolves to
    the wrong bin. A scan landing on the wrong container is worse than one landing
    on nothing.
    """
    doomed = make_location(db, name="Drawer A")
    location_tree(db).rebuild_paths()
    db.flush()
    document = Document(
        sha256="a" * 64,
        kind=DocumentKind.PHOTO,
        media_type="image/jpeg",
        byte_size=12,
        storage_path="aa/a" * 4,
    )
    db.add(document)
    db.flush()
    db.add(
        DocumentLink(
            document_id=document.id,
            entity_type=EntityType.LOCATION,
            entity_pk=doomed.id,
            role=DocumentRole.PHOTO,
            is_primary=True,
        )
    )
    db.add(
        BarcodeAlias(
            code_norm="DRAWERA123",
            symbology="code128",
            entity_type=EntityType.LOCATION,
            entity_pk=doomed.id,
        )
    )
    db.commit()
    doomed_id = doomed.id

    assert client.delete(f"/api/locations/{doomed_id}").status_code == 200
    db.expunge_all()

    links = db.execute(
        select(func.count())
        .select_from(DocumentLink)
        .where(
            DocumentLink.entity_type == EntityType.LOCATION,
            DocumentLink.entity_pk == doomed_id,
        )
    ).scalar_one()
    aliases = db.execute(
        select(func.count())
        .select_from(BarcodeAlias)
        .where(
            BarcodeAlias.entity_type == EntityType.LOCATION,
            BarcodeAlias.entity_pk == doomed_id,
        )
    ).scalar_one()
    assert (links, aliases) == (0, 0)

    # The rowid really is reused, which is what makes the assertion above matter
    # rather than being tidiness: this new, unrelated drawer is the old id.
    reused = make_location(db, name="Drawer B (brand new, unrelated)")
    location_tree(db).rebuild_paths()
    db.commit()
    assert reused.id == doomed_id
    fresh = client.get(f"/api/locations/{reused.id}").json()
    assert fresh["photo"] is None and fresh["effective_photo"] is None
    assert client.post("/api/scan/resolve", json={"code": "DRAWERA123"}).json()["status"] != (
        "resolved"
    )


# ---------------------------------------------------------------------------
# 5. A retired container is out of the tree — on the write side too
# ---------------------------------------------------------------------------


def test_stock_cannot_be_put_into_a_retired_container(client: TestClient, db: Session) -> None:
    """Retirement hides a container from every read, so a write into one strands
    stock where no screen can show it.

    `retire` takes the container out of the tree, its parent's canvas, the room
    plan and auto-assignment — so a lot received into it afterwards has a non-zero
    balance at a location that appears nowhere, cannot be found except by the tag
    still stuck to its front, and cannot be removed either, because `plan_removal`
    refuses on `holds_stock`. The station commits through these same routes and
    knows nothing about retirement, which is why the refusal is the server's job.

    Taking stock *out* keeps working: that is how somebody empties a drawer they
    have just removed.
    """
    part = make_part(db, name="Resistor", mpn="RC0603FR-071KL")
    live = make_location(db, name="Live drawer")
    retired = make_location(db, name="Retired drawer")
    lot = make_lot(db, part, retired, qty_milli=5_000)
    location_tree(db).rebuild_paths()
    db.commit()

    # It has history, so removal retires rather than deletes it. Emptying it first
    # is what makes it removable at all.
    moved = client.post(f"/api/stock/lots/{lot.id}/move", json={"to_location_id": live.id})
    assert moved.status_code == 200, moved.text
    assert client.delete(f"/api/locations/{retired.id}").json()["retired_location_ids"] == [
        retired.id
    ]

    refused = client.post(
        "/api/stock/receive",
        json={"part_id": part.id, "location_id": retired.id, "qty_milli": 500_000},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "location_retired"

    refused_move = client.post(
        f"/api/stock/lots/{lot.id}/move", json={"to_location_id": retired.id}
    )
    assert refused_move.status_code == 409, refused_move.text
    assert refused_move.json()["detail"]["reason"] == "location_retired"

    refused_empty = client.post(
        f"/api/stock/locations/{live.id}/empty", json={"to_location_id": retired.id}
    )
    assert refused_empty.status_code == 409, refused_empty.text
    assert refused_empty.json()["detail"]["reason"] == "location_retired"

    # Out of it still works — the drawer is removed, not sealed.
    out = client.post(f"/api/stock/locations/{retired.id}/empty", json={"to_location_id": live.id})
    assert out.status_code == 200, out.text


def test_nothing_can_be_created_inside_a_retired_container(client: TestClient, db: Session) -> None:
    """A live child under a retired parent is the one shape the tree read cannot
    draw.

    `read_location_tree` filters retired nodes row by row, on the stated grounds
    that a retired node's descendants are retired too — which `removal.restore`
    enforces from the other end. A live child of a retired parent breaks it: the
    child comes back from `/tree` and its parent does not, so the client re-roots
    it and it renders as a **top-level** container whose `label_path` still names
    the cabinet somebody removed. Auto-assignment would then offer it as somewhere
    to put stock.
    """
    part = make_part(db, name="Diode", mpn="1N4148")
    cabinet = make_location(db, name="Cabinet C")
    lot = make_lot(db, part, cabinet, qty_milli=0)
    assert lot.qty_milli_cached == 0
    location_tree(db).rebuild_paths()
    db.commit()

    assert client.delete(f"/api/locations/{cabinet.id}").json()["retired_location_ids"] == [
        cabinet.id
    ]

    refused = client.post("/api/locations", json={"name": "Drawer D", "parent_id": cabinet.id})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "parent_retired"

    container_type = make_container_type(db, slug="raaco-12")
    db.commit()
    refused_stamp = client.post(
        f"/api/locations/{cabinet.id}/instantiate",
        json={"container_type_id": container_type.id, "count": 1, "naming_pattern": "D{n}"},
    )
    assert refused_stamp.status_code == 409, refused_stamp.text
    assert refused_stamp.json()["detail"]["reason"] == "parent_retired"

    # Restored, it takes children again — the refusal is about state, not identity.
    assert client.post(f"/api/locations/{cabinet.id}/restore").status_code == 200
    again = client.post("/api/locations", json={"name": "Drawer D", "parent_id": cabinet.id})
    assert again.status_code == 201, again.text
