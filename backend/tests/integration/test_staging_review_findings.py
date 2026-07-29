"""Regressions for the defects adversarial review found in the ADR 0004 staging work.

Five of them, and four share one root cause worth naming before the individual
docstrings: **`STAGED` was treated as a terminal statement about the past when it
is a claim about the present.** `CONSUMED` really is terminal — the parts are
soldered in and no later recount can un-pick them — so crediting it whole and
unconditionally is right. A `STAGED` row instead asserts *these parts are in that
box right now*, which a recount, an ordinary move or a partial build can all
falsify afterwards. Every place that copied `CONSUMED`'s treatment onto `STAGED`
reintroduced the exact bug the previous review round fixed for `RESERVED`
(`test_phase2_review_findings.test_an_emptied_bin_makes_the_build_short_not_buildable`):
a build reporting itself buildable off stock that is not there.

The fifth is a shape this repo keeps producing: **an ordering fix standing in for
a predicate fix.** `delete_project` documented that a removable parent must not be
attempted under a retained child, and implemented it by sorting deepest-first —
which only helps when both are removable, and 500s when the child is the one
holding the lot.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import AllocationState, ShortageKind
from app.models.projects import StockAllocation
from app.models.storage import Location
from app.services import ledger, reservations, staging
from tests.factories import (
    make_bom_line,
    make_build,
    make_location,
    make_lot,
    make_part,
    make_project,
)

ATTRIBUTION = ledger.Attribution()


# ---------------------------------------------------------------------------
# 1. Deleting a project whose parts only ever went to an assembly box 500'd
# ---------------------------------------------------------------------------


def test_deleting_a_project_staged_only_into_an_assembly_succeeds(
    client: TestClient, db: Session
) -> None:
    """The defect: ADR 0004's headline gesture — "send them to one of its
    assemblies" — made the project permanently undeletable with a 500.

    The tree is `PROJECTS / Blinky / Build 1 Assembly 1`. The lot lives at the
    *assembly* node, so the project node in the middle is named by no lot and no
    ledger row and `_location_is_referenced` calls it removable — while its child
    is retained. `locations.parent_id` is `RESTRICT`, so the parent's delete
    raised `IntegrityError` at commit, the whole transaction rolled back, and the
    project survived every attempt.

    Deepest-first ordering was standing in for the predicate: removable has to
    mean "and nothing retained sits under me". Staging floating parts first masks
    it, because then the project node holds a lot of its own and is retained too.
    """
    project = make_project(db, name="Blinky", revision="v1")
    build = make_build(db, project, assembly_count=1)
    part = make_part(db)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=10_000)
    db.commit()

    staged = client.post(
        f"/api/builds/{build.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 4_000, "assembly_no": 1},
    )
    assert staged.status_code == 200, staged.text
    built = client.post(
        f"/api/builds/{build.id}/consume-staged",
        json={"allocation_id": staged.json()["allocation"]["id"], "qty_milli": 4_000},
    )
    assert built.status_code == 200, built.text

    response = client.delete(f"/api/projects/{project.id}")

    assert response.status_code == 200, response.text
    assert client.get(f"/api/projects/{project.id}").status_code == 404
    # The assembly box is named by undeletable ledger rows, so it stays — and
    # therefore so must its parent, whatever the reference check says about the
    # parent alone.
    removed = response.json()["removed_location_ids"]
    project_node = db.execute(
        select(Location).where(Location.slot_label == f"P{project.id}")
    ).scalar_one()
    assert project_node.id not in removed
    assert db.get(Location, project_node.id) is not None


def test_an_untouched_staging_branch_is_still_cleaned_up(client: TestClient, db: Session) -> None:
    """The fix must not turn "usually none removed" into "never any removed".

    A branch materialised by a *refused* withdrawal holds no lot and names no
    ledger row anywhere in it, so the whole thing goes — that is the case the
    reference check was written for, and a blanket "keep every ancestor" would
    have silently dropped it."""
    project = make_project(db)
    build = make_build(db, project, assembly_count=2)

    # Materialise the branch without any movement through it: the service
    # creates the nodes, and nothing else ever names them.
    node = staging.assembly_staging_location(db, build, project, 1)
    db.commit()
    branch = {node.id, node.parent_id}

    response = client.delete(f"/api/projects/{project.id}")

    assert response.status_code == 200, response.text
    assert branch <= set(response.json()["removed_location_ids"])
    # Re-read past the identity map: the route ran in its own session, so `get`
    # would otherwise hand back this session's copy of a row that is gone.
    db.expire_all()
    assert db.execute(select(Location.id).where(Location.id.in_(branch))).all() == []


# ---------------------------------------------------------------------------
# 2. A second top-level `PROJECTS` slot label broke every staging read
# ---------------------------------------------------------------------------


def test_a_users_own_top_level_projects_label_does_not_become_the_staging_root(
    client: TestClient, db: Session
) -> None:
    """The defect: `staging_root` looked up `(parent_id IS NULL, 'PROJECTS')` with
    `scalar_one_or_none`, and `POST /api/locations` accepts `slot_label` freely.

    `UNIQUE(parent_id, slot_label)` does not bind at the top level — SQL treats
    NULLs in a unique index as distinct — so a user naming a shelf `PROJECTS`
    produced two rows and every staging read raised `MultipleResultsFound`: 500s
    from `/shortages`, `/pick-list`, `/allocate` and `DELETE /api/projects/{id}`
    at once. The module docstring justified the missing DB guarantee with
    "single-writer by construction", which covers a race, not a second row a user
    deliberately creates.

    The quieter half of the same bug: created *first*, that shelf was **adopted**
    as the staging root, so its whole subtree stopped counting as available and
    `reserve` refused it — a build reporting a shortage of parts sitting on the
    shelf in front of you.
    """
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    line = make_bom_line(db, project, qty_per_assembly_milli=1_000, part_id=part.id)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=10_000)
    db.commit()

    staged = client.post(
        f"/api/builds/{build.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 1_000, "bom_line_id": line.id},
    )
    assert staged.status_code == 200, staged.text

    # A human shelf that happens to be labelled the same thing. Whether this is
    # refused or merely kept distinct, what must not happen is a 500.
    clash = client.post("/api/locations", json={"name": "Project shelf", "slot_label": "PROJECTS"})
    assert clash.status_code in (201, 409), clash.text

    assert client.get(f"/api/builds/{build.id}/shortages").status_code == 200
    assert client.get(f"/api/builds/{build.id}/pick-list").status_code == 200
    held = client.post(
        f"/api/builds/{build.id}/allocate", json={"lot_id": lot.id, "qty_milli": 1_000}
    )
    assert held.status_code == 200, held.text
    assert client.delete(f"/api/projects/{project.id}").status_code == 409


def test_a_pre_existing_top_level_projects_label_is_not_adopted(
    client: TestClient, db: Session
) -> None:
    """The numeric half, and the quieter one: **before** any staging existed,
    the user's own `PROJECTS`-labelled shelf was adopted as the staging root, so
    every lot under it stopped counting as available and `reserve` refused it.

    Built through the locations API rather than the factory so the shelf's
    `id_path` is a real cached path — the whole exclusion is a prefix match on
    that column, and a factory row with an empty `id_path` would pass this test
    no matter which node the root turned out to be.
    """
    shelf = client.post("/api/locations", json={"name": "Project shelf", "slot_label": "PROJECTS"})
    assert shelf.status_code == 201, shelf.text
    shelf_id = shelf.json()["location"]["id"]
    shelf_bin = client.post(
        "/api/locations", json={"name": "Shelf bin", "parent_id": shelf_id, "slot_label": "A1"}
    )
    assert shelf_bin.status_code == 201, shelf_bin.text

    part = make_part(db)
    make_lot(db, part, db.get_one(Location, shelf_bin.json()["location"]["id"]), qty_milli=50_000)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=10_000)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    line = make_bom_line(db, project, qty_per_assembly_milli=1_000, part_id=part.id)
    db.commit()

    response = client.post(
        f"/api/builds/{build.id}/stage", json={"lot_id": lot.id, "qty_milli": 1_000}
    )
    assert response.status_code == 200, response.text
    db.expire_all()

    root = staging.staging_root(db, create=False)
    assert root is not None
    assert root.id != shelf_id, "a user's shelf is not the staging root"
    # 50 000 on the shelf plus the 9 000 left in the drawer: every unit that is
    # not actually in a project box.
    assert reservations.available_by_part(db, [part.id])[part.id] == 59_000
    # And the write path agrees with the report, which is the invariant that
    # matters: a shortage nobody can act on is worse than no report.
    assert (
        client.post(
            f"/api/builds/{build.id}/allocate",
            json={"lot_id": lot.id, "qty_milli": 1_000, "bom_line_id": line.id},
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# 3. A staged row was credited whole no matter what its box now holds
# ---------------------------------------------------------------------------


def test_a_staging_box_recounted_to_empty_makes_the_build_short(
    client: TestClient, db: Session
) -> None:
    """The defect: stage the whole lot into the project box, recount that box to
    zero, and `/shortages` still said `satisfied` / `is_buildable: true` with an
    empty database of stock — while `/unstage` on the very same allocation
    refused with `partly_consumed` because the box holds nothing.

    This is `test_an_emostied_bin` all over again through a new state.
    `_holdings_by_line` built its `undeliverable` figure from `RESERVED` rows
    only, so a `STAGED` row skipped the headroom check entirely. The reasoning
    that excuses `CONSUMED` — "no headroom question to ask about a lot they are
    no longer in" — is false for `STAGED`: the parts are asserted to be *in* a
    lot that still has a balance, and `unstage` tests exactly that balance.
    """
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    line = make_bom_line(db, project, qty_per_assembly_milli=4_000, part_id=part.id)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=4_000)
    db.commit()

    staged = client.post(
        f"/api/builds/{build.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 4_000, "bom_line_id": line.id},
    )
    assert staged.status_code == 200, staged.text
    staging_lot_id = staged.json()["staging_lot"]["id"]

    # The box turns out to be empty. Somebody has to sort that out, but until
    # they do, the build cannot be built.
    recount = client.post(
        f"/api/stock/lots/{staging_lot_id}/recount", json={"counted_qty_milli": 0}
    )
    assert recount.status_code == 200, recount.text

    report = client.get(f"/api/builds/{build.id}/shortages")
    assert report.status_code == 200, report.text
    body = report.json()
    (shortage,) = body["lines"]
    assert body["is_buildable"] is False
    assert shortage["kind"] == ShortageKind.SHORT.value
    assert shortage["staged_milli"] == 4_000  # the claim is still on the books
    assert shortage["undeliverable_milli"] == 4_000  # and every unit of it is fiction
    assert shortage["shortfall_milli"] == 4_000
    assert shortage["is_blocking"] is True

    # The pick list inherits `needed_milli` from the same report, so the gap has
    # to reappear there too — that is the screen someone acts on.
    picks = client.get(f"/api/builds/{build.id}/pick-list")
    assert picks.status_code == 200, picks.text
    assert picks.json()["is_complete"] is False
    assert [gap["needed_milli"] for gap in picks.json()["gaps"]] == [4_000]


def test_a_staged_row_whose_box_still_holds_it_is_still_satisfied(db: Session) -> None:
    """The other direction, so the fix above cannot be "always report short".

    Held separately from the whole-lot case because a partial withdrawal mints a
    *second* lot at the destination, and only one of the two shapes would catch a
    fix that checked the source lot's balance by mistake."""
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    line = make_bom_line(db, project, qty_per_assembly_milli=4_000, part_id=part.id)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=10_000)

    reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION, bom_line=line)
    db.flush()

    report = reservations.shortage_for_build(db, build).lines[0]
    assert report.kind is ShortageKind.SATISFIED
    assert report.staged_milli == 4_000
    assert report.undeliverable_milli == 0
    assert report.needed_milli == 0


# ---------------------------------------------------------------------------
# 4. Stock moved out of a staging box was counted on both sides of the netting
# ---------------------------------------------------------------------------


def test_stock_moved_out_of_staging_is_not_counted_twice(client: TestClient, db: Session) -> None:
    """The defect: following `unstage`'s own instruction double-counted the stock.

    A partial `consume-staged` leaves a `STAGED` remainder with no
    `staged_ledger_seq`, and `unstage` refuses it with "move the stock back from
    its staging location instead". Do exactly that, and the units are counted
    twice: once as line 1's `staged_milli` (holding its `needed_milli` at zero)
    and once as free pool for line 2, because the lot left the staging subtree so
    `available_by_part` stopped excluding it. Line 2's reported shortfall
    understated the real one by the whole remainder — 3 000 rather than 8 000.

    A staged row whose lot is no longer inside the staging prefix is a claim with
    no physical fact behind it, so it belongs in `undeliverable`. The units are
    then counted on exactly one side: line 1's `needed_milli` comes back up to
    5 000 and it draws that from the pool, leaving line 2 the 2 000 that is
    genuinely left rather than the whole 7 000.
    """
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    line_one = make_bom_line(db, project, qty_per_assembly_milli=8_000, part_id=part.id)
    line_two = make_bom_line(db, project, qty_per_assembly_milli=10_000, part_id=part.id)
    bin_a = make_location(db, "Bin A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    db.commit()

    staged = client.post(
        f"/api/builds/{build.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 8_000, "bom_line_id": line_one.id},
    )
    assert staged.status_code == 200, staged.text
    staging_lot_id = staged.json()["staging_lot"]["id"]
    built = client.post(
        f"/api/builds/{build.id}/consume-staged",
        json={"allocation_id": staged.json()["allocation"]["id"], "qty_milli": 3_000},
    )
    assert built.status_code == 200, built.text

    moved = client.post(
        f"/api/stock/lots/{staging_lot_id}/move",
        json={"to_location_id": bin_a.id, "qty_milli": 5_000},
    )
    assert moved.status_code == 200, moved.text

    report = client.get(f"/api/builds/{build.id}/shortages")
    assert report.status_code == 200, report.text
    by_line = {line["bom_line_id"]: line for line in report.json()["lines"]}

    # Line 1's claim on 5 000 units is fiction — they are back in Bin A and
    # nothing sets them aside — so the claim is reported as undeliverable and the
    # requirement it was covering comes back as needed.
    one = by_line[line_one.id]
    assert one["staged_milli"] == 5_000
    assert one["undeliverable_milli"] == 5_000
    assert one["needed_milli"] == 5_000
    assert one["consumed_milli"] == 3_000

    # And the 7 000 really in Bin A is now spent on exactly one side of the
    # netting: line 1 draws the 5 000 it needs, line 2 sees the 2 000 left. The
    # defect had line 2 seeing all 7 000 *while* line 1 still claimed 5 000 of
    # them, understating its shortfall by exactly the remainder.
    two = by_line[line_two.id]
    assert two["available_milli"] == 2_000
    assert two["shortfall_milli"] == 8_000
    assert two["is_blocking"] is True
    assert report.json()["is_buildable"] is False


def test_a_staged_lot_relocated_whole_out_of_staging_is_not_counted_twice(
    client: TestClient, db: Session
) -> None:
    """The same defect through a **whole-lot** move, which is the shape the
    headroom check alone cannot catch.

    A partial move out of a project box drains the staging lot to zero, so the
    balance test already calls the claim undeliverable and the prefix test looks
    redundant. A whole-lot move does not: the lot keeps its identity, keeps its
    4 000, and only changes `location_id`. So its balance still backs the claim
    while `available_by_part` has started counting it as free stock — the double
    count, with nothing about the numbers on the lot to reveal it. Position in the
    tree is the only thing that changed, so position is what has to be tested.
    """
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    line_one = make_bom_line(db, project, qty_per_assembly_milli=4_000, part_id=part.id)
    line_two = make_bom_line(db, project, qty_per_assembly_milli=10_000, part_id=part.id)
    make_lot(db, part, make_location(db, "Bin B"), qty_milli=0)
    lot = make_lot(db, part, make_location(db, "Drawer A"), qty_milli=10_000)
    db.commit()

    staged = client.post(
        f"/api/builds/{build.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 4_000, "bom_line_id": line_one.id},
    )
    assert staged.status_code == 200, staged.text
    staging_lot_id = staged.json()["staging_lot"]["id"]

    # No `qty_milli`: the whole lot relocates, keeping its balance and its id.
    bin_b = db.execute(select(Location).where(Location.name == "Bin B")).scalar_one()
    moved = client.post(f"/api/stock/lots/{staging_lot_id}/move", json={"to_location_id": bin_b.id})
    assert moved.status_code == 200, moved.text
    assert moved.json()["lot"]["qty_milli"] == 4_000, "the lot kept its balance"

    by_line = {
        row["bom_line_id"]: row
        for row in client.get(f"/api/builds/{build.id}/shortages").json()["lines"]
    }
    assert by_line[line_one.id]["undeliverable_milli"] == 4_000
    assert by_line[line_one.id]["needed_milli"] == 4_000
    # 10 000 real units against 14 000 of demand. Line 1 draws its 4 000 back out
    # of the pool, so line 2 sees 6 000 — not the 10 000 that would have made the
    # same units cover both lines.
    assert by_line[line_two.id]["available_milli"] == 6_000
    assert by_line[line_two.id]["shortfall_milli"] == 4_000


# ---------------------------------------------------------------------------
# 5. The staged remainder no operation could clear
# ---------------------------------------------------------------------------


def test_a_staged_remainder_whose_parts_left_can_be_released(
    client: TestClient, db: Session
) -> None:
    """The defect: three refusals pointing at each other left a row stuck forever.

    `release` refused `STAGED` ("un-stage it"), `unstage` refused a remainder with
    no `staged_ledger_seq` ("move the stock back"), and `release_build` selects
    `PLANNED`/`RESERVED` only — so after following the documented instruction the
    only reachable transition was `consume-staged`, which would assert parts were
    soldered in that were not. Closing the build left the row, permanently
    feeding the double-count above.

    Releasing it is honest precisely *because* the parts have provably left the
    staging subtree: there is no physical fact left for the bookkeeping change to
    contradict, which is the whole reason `release` refuses a staged row that is
    still in its box.
    """
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    line = make_bom_line(db, project, qty_per_assembly_milli=8_000, part_id=part.id)
    bin_a = make_location(db, "Bin A")
    lot = make_lot(db, part, bin_a, qty_milli=10_000)
    db.commit()

    staged = client.post(
        f"/api/builds/{build.id}/stage",
        json={"lot_id": lot.id, "qty_milli": 8_000, "bom_line_id": line.id},
    )
    allocation_id = staged.json()["allocation"]["id"]
    client.post(
        f"/api/builds/{build.id}/consume-staged",
        json={"allocation_id": allocation_id, "qty_milli": 3_000},
    )
    remainder_id = db.execute(
        select(StockAllocation.id)
        .where(StockAllocation.state == AllocationState.STAGED)
        .order_by(StockAllocation.id.desc())
    ).scalar_one()
    client.post(
        f"/api/stock/lots/{staged.json()['staging_lot']['id']}/move",
        json={"to_location_id": bin_a.id, "qty_milli": 5_000},
    )

    released = client.post(f"/api/builds/{build.id}/release", json={"allocation_id": remainder_id})

    assert released.status_code == 200, released.text
    db.expire_all()
    assert db.get_one(StockAllocation, remainder_id).state == AllocationState.RELEASED
    # And the free pool is now unambiguous: 7 000 in Bin A, claimed by nobody.
    assert reservations.available_by_part(db, [part.id])[part.id] == 7_000
    report = client.get(f"/api/builds/{build.id}/shortages").json()
    assert report["lines"][0]["staged_milli"] == 0


def test_a_staged_row_still_in_its_box_is_still_not_releasable(db: Session) -> None:
    """The refusal that matters is unchanged: dropping the claim while the parts
    sit in the project box would leave real stock nothing accounts for."""
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    lot = make_lot(db, part, make_location(db, "Bin A"), qty_milli=10_000)
    move = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION)
    db.flush()

    try:
        reservations.release(db, move.allocation)
    except reservations.ReservationError as error:
        assert error.reason == "is_staged"
    else:  # pragma: no cover - the assertion above is the test
        raise AssertionError("releasing a staged row in its box must be refused")


def test_release_build_clears_a_stranded_staged_remainder(client: TestClient, db: Session) -> None:
    """Closing a build must not leave a row behind that nothing can ever clear —
    that is how the stranded remainder became permanent in the first place. Rows
    whose parts are still in the box stay put, because abandoning a build does not
    move anything off a shelf."""
    part = make_part(db)
    project = make_project(db)
    build = make_build(db, project, assembly_count=1)
    bin_a = make_location(db, "Bin A")
    lot = make_lot(db, part, bin_a, qty_milli=20_000)
    gone = reservations.stage(db, build, lot, 8_000, attribution=ATTRIBUTION)
    reservations.consume_staged(db, gone.allocation, attribution=ATTRIBUTION, qty_milli=3_000)
    stays = reservations.stage(db, build, lot, 4_000, attribution=ATTRIBUTION, assembly_no=1)
    db.commit()

    stranded = db.execute(
        select(StockAllocation)
        .where(StockAllocation.state == AllocationState.STAGED)
        .order_by(StockAllocation.id)
    ).scalars()
    remainder = next(row for row in stranded if row.staged_ledger_seq is None)
    moved = client.post(
        f"/api/stock/lots/{remainder.lot_id}/move",
        json={"to_location_id": bin_a.id, "qty_milli": 5_000},
    )
    assert moved.status_code == 200, moved.text

    released = client.post(f"/api/builds/{build.id}/release", json={})

    assert released.status_code == 200, released.text
    db.expire_all()
    assert db.get_one(StockAllocation, remainder.id).state == AllocationState.RELEASED
    assert db.get_one(StockAllocation, stays.allocation.id).state == AllocationState.STAGED
