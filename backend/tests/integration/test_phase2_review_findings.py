"""Regressions for the eight defects adversarial review found in Phase 2a.

Each was reproduced first, and every one of them is a **quiet** failure: not one
would have shown up as an error, a log line or a red test. That is what they have
in common and why they are collected here rather than filed under the feature
each belongs to.

They fall into three shapes worth naming, because the next batch of code will
have the same opportunities:

* **a derived number computed two ways.** The shortage report credited a hold
  that `available_by_part` had already thrown away, so a build reported itself
  buildable off a bin that had just been recounted to zero. Same family: the
  reserved cache was maintained by a service the DB's own `ON DELETE CASCADE`
  never calls.
* **a machine guess presented as a fact.** The value parser was supposed to be
  the gate that stops a `Value` cell being used as a part number, and it only was
  when the designator happened to begin with `R`, `C` or `L`. `RN1 / 10k` (a
  resistor network) matched a chip resistor; `10k 1%` matched a 10.1 kΩ E96 part.
* **input inside every documented bound reaching an unhandled exception.** A
  200 000-character CSV cell — a fifth of what the route accepts — hit `csv`'s
  undocumented 131072-character field cap and 500'd a module whose contract is
  that it never raises.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.maintenance import check_reserved_quantity_drift, rebuild_reserved_quantities
from app.models.enums import LotStatus, ShortageKind
from app.models.projects import StockAllocation
from app.services import reservations
from app.services.bom_import import import_bom, parse_bom, rematch_project
from tests.factories import (
    make_bom_line,
    make_build,
    make_location,
    make_lot,
    make_part,
    make_project,
)

# ---------------------------------------------------------------------------
# 1. A hold whose lot cannot fill it was credited to the line anyway
# ---------------------------------------------------------------------------


def test_an_emptied_bin_makes_the_build_short_not_buildable(db: Session) -> None:
    """The defect: receive 100, reserve all 100, recount the bin to 0 — and the
    line still said `satisfied`, `is_buildable` still said `true`.

    `available_by_part` clamps each lot at zero, so the over-committed lot
    contributed `max(0, 0 - 100) = 0` free and the −100 vanished there; the hold
    was then added back whole as `allocated_milli`, crediting the same
    milli-units twice. The report that decides whether a build can go ahead was
    the one place the anomaly was invisible.
    """
    part = make_part(db, mpn="EMPTY-BIN")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=100_000)
    project = make_project(db)
    line = make_bom_line(db, project, qty_per_assembly_milli=100_000, part_id=part.id)
    build = make_build(db, project)
    reservations.reserve(db, build, lot, 100_000, bom_line=line)

    # The bin is physically empty. The hold is still on the books, which is
    # correct — somebody has to release it — but it can no longer be filled.
    lot.qty_milli_cached = 0
    db.flush()

    report = reservations.shortage_for_build(db, build)
    (shortage,) = report.lines
    assert shortage.kind is ShortageKind.SHORT
    assert shortage.allocated_milli == 100_000
    assert shortage.undeliverable_milli == 100_000
    assert shortage.shortfall_milli == 100_000
    assert report.is_buildable is False


def test_a_deliverable_hold_still_satisfies_its_line(db: Session) -> None:
    """The fix must not have turned every hold into a shortage: a hold backed by
    stock that is really in the bin reduces the requirement exactly as before, and
    reports nothing undeliverable."""
    part = make_part(db, mpn="GOOD-HOLD")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=100_000)
    project = make_project(db)
    line = make_bom_line(db, project, qty_per_assembly_milli=100_000, part_id=part.id)
    build = make_build(db, project)
    reservations.reserve(db, build, lot, 100_000, bom_line=line)

    report = reservations.shortage_for_build(db, build)
    (shortage,) = report.lines
    assert shortage.kind is ShortageKind.SATISFIED
    assert shortage.undeliverable_milli == 0
    assert report.is_buildable is True


def test_a_hold_on_quarantined_stock_does_not_satisfy_a_line(db: Session) -> None:
    """Same root cause, reached from the other side: `available_by_part` drops a
    non-`ACTIVE` lot entirely — `reserve` refuses those lots for the same reason —
    while the hold on it was still credited. Quarantined stock is physically
    present and must not be promised to a build."""
    part = make_part(db, mpn="QUARANTINE")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=100_000)
    project = make_project(db)
    line = make_bom_line(db, project, qty_per_assembly_milli=100_000, part_id=part.id)
    build = make_build(db, project)
    reservations.reserve(db, build, lot, 100_000, bom_line=line)

    lot.status = LotStatus.QUARANTINED
    db.flush()

    (shortage,) = reservations.shortage_for_build(db, build).lines
    assert shortage.kind is ShortageKind.SHORT
    assert shortage.undeliverable_milli == 100_000


def test_two_builds_over_committing_one_lot_are_both_told_they_are_short(db: Session) -> None:
    """There is no priority rule between two builds promised the same parts, so
    each is credited only what is left after the *other* holder — the direction
    that over-reports a shortage. Over-reporting is visible in this report and
    released with one action; under-reporting is discovered at the bench."""
    part = make_part(db, mpn="OVERCOMMIT")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=100_000)
    project = make_project(db)
    line = make_bom_line(db, project, qty_per_assembly_milli=100_000, part_id=part.id)
    first = make_build(db, project, build_no=1)
    second = make_build(db, project, build_no=2)
    reservations.reserve(db, first, lot, 100_000, bom_line=line)
    reservations.reserve(db, second, lot, 100_000, bom_line=line, allow_overcommit=True)

    for build in (first, second):
        (shortage,) = reservations.shortage_for_build(db, build).lines
        assert shortage.kind is ShortageKind.SHORT, f"build {build.build_no}"
        assert shortage.undeliverable_milli == 100_000


def test_a_consumed_pick_is_credited_even_if_the_lot_is_emptied_afterwards(
    db: Session,
) -> None:
    """A `CONSUMED` row is not a promise — the parts left the bin and
    `stock_ledger` says so. Capping it against the lot's *current* balance would
    make every finished build retroactively short.

    The lot here is emptied *and* over-committed by somebody else afterwards, so
    there is genuinely no headroom left: if the pick were treated like a hold it
    would be credited nothing and this finished line would report `short`.
    """
    part = make_part(db, mpn="ALREADY-PICKED")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=10_000)
    project = make_project(db)
    line = make_bom_line(db, project, qty_per_assembly_milli=4_000, part_id=part.id)
    build = make_build(db, project)
    other = make_build(db, project, build_no=2)
    allocation = reservations.reserve(db, build, lot, 4_000, bom_line=line)
    reservations.consume(db, allocation, attribution=reservations.ledger.Attribution())

    # Whatever happens to the bin afterwards, those four are on the board.
    lot.qty_milli_cached = 0
    db.flush()
    reservations.reserve(db, other, lot, 5_000, allow_overcommit=True)

    (shortage,) = reservations.shortage_for_build(db, build).lines
    assert shortage.kind is ShortageKind.SATISFIED
    assert shortage.allocated_milli == 4_000
    assert shortage.undeliverable_milli == 0


def test_the_shortage_route_reports_the_undeliverable_hold(client: TestClient) -> None:
    """End to end over HTTP, which is how it was found: the client needs the
    number, or `100 required / 100 held / short 100` reads as a contradiction."""
    part = client.post(
        "/api/parts", json={"name": "R 10k", "mpn": "HTTP-EMPTY", "part_kind_slug": "component"}
    ).json()["part"]
    location = client.post("/api/locations", json={"name": "Probe bin"}).json()["location"]
    lot_id = client.post(
        "/api/stock/receive",
        json={"part_id": part["id"], "location_id": location["id"], "qty_milli": 100_000},
    ).json()["lot"]["id"]
    project = client.post("/api/projects", json={"name": "Probe board"}).json()["project"]
    imported = client.post(
        f"/api/projects/{project['id']}/bom/import",
        json={"content": "Reference,Value,MPN,Qty\nR1,10k,HTTP-EMPTY,100\n"},
    ).json()
    assert imported["matched_count"] == 1
    build = client.post(f"/api/projects/{project['id']}/builds", json={"assembly_count": 1}).json()[
        "build"
    ]
    allocated = client.post(
        f"/api/builds/{build['id']}/allocate",
        json={
            "lot_id": lot_id,
            "qty_milli": 100_000,
            "bom_line_id": imported["lines"][0]["id"],
        },
    )
    assert allocated.status_code == 200, allocated.text
    emptied = client.post(f"/api/stock/lots/{lot_id}/recount", json={"counted_qty_milli": 0})
    assert emptied.status_code == 200, emptied.text

    report = client.get(f"/api/builds/{build['id']}/shortages").json()
    assert report["is_buildable"] is False
    (line,) = report["lines"]
    assert line["kind"] == "short"
    assert line["undeliverable_milli"] == 100_000
    assert line["shortfall_milli"] == 100_000


# ---------------------------------------------------------------------------
# 2. Deleting a build cascaded RESERVED rows away without moving the cache
# ---------------------------------------------------------------------------


def _reserved_agrees(db: Session, lot_id: int) -> None:
    """The cache, the per-lot recomputation and the bulk rebuild, all three."""
    lot = db.get(reservations.StockLot, lot_id)
    assert lot is not None
    db.expire(lot)
    cached = lot.qty_reserved_milli_cached
    assert cached == reservations.reserved_milli(db, lot_id)
    assert check_reserved_quantity_drift(db).is_clean
    rebuild_reserved_quantities(db)
    db.flush()
    db.expire(lot)
    assert lot.qty_reserved_milli_cached == cached


def test_deleting_a_build_gives_its_reservations_back(db: Session) -> None:
    """The defect: `stock_allocations.build_id` is `ON DELETE CASCADE`, so SQLite
    removed the `RESERVED` rows itself and no service ran — the counter stayed at
    4000 with zero allocation rows behind it, hiding 4000 of real stock behind a
    hold nothing could ever release.

    Note the asymmetry this closes: `stock_ledger` cannot be deleted at all (its
    triggers `RAISE(ABORT)`), which is why `qty_milli_cached` never needed the
    equivalent protection and why the absence of it here was easy to miss.
    """
    part = make_part(db, mpn="DELETED-BUILD")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=10_000)
    project = make_project(db)
    build = make_build(db, project)
    reservations.reserve(db, build, lot, 4_000)
    db.commit()
    assert lot.qty_reserved_milli_cached == 4_000

    db.delete(build)
    db.commit()

    assert db.query(StockAllocation).count() == 0
    db.expire(lot)
    assert lot.qty_reserved_milli_cached == 0
    assert reservations.available(lot) == 10_000
    _reserved_agrees(db, lot.id)


def test_deleting_a_project_gives_its_builds_reservations_back(db: Session) -> None:
    """The same hole through the chained cascade — project to builds to
    allocations — which is the reachable one, since a project delete is already
    exercised as supported behaviour in `test_projects_schema`."""
    part = make_part(db, mpn="DELETED-PROJECT")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=10_000)
    project = make_project(db)
    build = make_build(db, project)
    reservations.reserve(db, build, lot, 6_000)
    db.commit()

    db.delete(project)
    db.commit()

    db.expire(lot)
    assert lot.qty_reserved_milli_cached == 0
    _reserved_agrees(db, lot.id)


def test_deleting_a_released_allocation_does_not_decrement_twice(db: Session) -> None:
    """The trigger's `WHEN` clause is load-bearing: `release` has already
    decremented a `RELEASED` row, so decrementing again on delete would drive the
    counter negative — exactly the double-decrement the release path refuses."""
    part = make_part(db, mpn="RELEASED-THEN-DELETED")
    location = make_location(db)
    lot = make_lot(db, part, location, qty_milli=10_000)
    project = make_project(db)
    build = make_build(db, project)
    allocation = reservations.reserve(db, build, lot, 4_000)
    reservations.release(db, allocation)
    db.commit()
    assert lot.qty_reserved_milli_cached == 0

    db.execute(text("DELETE FROM stock_allocations WHERE id = :id"), {"id": allocation.id})
    db.commit()

    db.expire(lot)
    assert lot.qty_reserved_milli_cached == 0
    _reserved_agrees(db, lot.id)


# ---------------------------------------------------------------------------
# 3. The value-parser gate only fired for an R/C/L designator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "content"),
    [
        # No designator column at all: `refs` is empty, so no quantity was
        # implied and the cell went straight through `normalize_mpn`.
        ("no reference column", "Value,Qty\n10k,5\n"),
        ("blank designator cell", "Reference,Value,Qty\n,10k,5\n"),
        # `RN` is a resistor network and `VR` a potentiometer. Neither prefix is
        # in the R/C/L table, and both were matched to a chip resistor.
        ("resistor network", "Reference,Value,Qty\nRN1,10k,1\n"),
        ("potentiometer", "Reference,Value,Qty\nVR1,10k,1\n"),
        ("mixed prefixes", 'Reference,Value,Qty\n"R1,C2",10k,2\n'),
    ],
)
def test_a_passive_value_is_never_an_mpn_key_whatever_the_designators_say(
    db: Session, label: str, content: str
) -> None:
    """The module's own regression test for this covered only the plain-`R` happy
    path, so the parser was the gate exactly when the designator cell cooperated.
    A value that reads as a quantity under *any* known quantity is a value, and
    the designators are no longer the only input to that decision.
    """
    make_part(db, name="Yageo 10k 0603 1%", mpn="10K")
    project = make_project(db)

    result = import_bom(db, project, parse_bom(content))

    assert result.matched_count == 0, label
    assert [line.part_id for line in result.lines] == [None], label


def test_a_tolerance_suffix_does_not_flatten_a_value_into_someone_elses_mpn(
    db: Session,
) -> None:
    """`normalize_mpn` deletes the space and the percent sign, so `10k 1%` became
    the key `10k1` — and `10K1` is a real E96 part number for 10.1 kΩ. A 10.0 kΩ
    line silently allocated 10.1 kΩ stock. `4k7 5%` against `4K75` is the same
    collision, and `10k/1%` and `10k, 1%` reduce to the same key.
    """
    make_part(db, name="Yageo 10.1k 0603 0.1%", mpn="10K1")
    make_part(db, name="Yageo 4.75k 0603 0.1%", mpn="4K75")
    project = make_project(db)

    result = import_bom(
        db,
        project,
        parse_bom("Reference,Value,Qty\nR1,10k 1%,1\nR2,4k7 5%,1\nR3,10k/1%,1\n"),
    )

    assert result.matched_count == 0
    assert [line.part_id for line in result.lines] == [None, None, None]


def test_a_part_number_in_the_value_column_still_matches(db: Session) -> None:
    """The feature the gate exists to protect, not just the refusal: a BOM with no
    MPN column at all is the most common shape of a hobby BOM, and these cells
    read as a value under nothing the parser knows."""
    for mpn in ("LM358N", "74HC595", "1N4148", "RC0603FR-0710KL", "STM32F103C8T6"):
        part = make_part(db, name=mpn, mpn=mpn)
        project = make_project(db, name=f"board {mpn}")
        result = import_bom(db, project, parse_bom(f"Reference,Value,Qty\nU1,{mpn},1\n"))
        assert result.matched_count == 1, mpn
        assert result.lines[0].part_id == part.id, mpn


def test_a_rematch_applies_the_same_gate_as_the_import(db: Session) -> None:
    """`rematch_project` rebuilds its candidates from the stored columns through
    the same `_mpn_candidates`, so a gate that only fixed the import path would
    have made the bad match on the next rematch pass instead."""
    project = make_project(db)
    result = import_bom(db, project, parse_bom("Reference,Value,Qty\nRN1,10k,1\n"))
    assert result.matched_count == 0

    # The part appears *after* the import, which is the whole point of rematching.
    make_part(db, name="Yageo 10k", mpn="10K")
    db.flush()

    assert rematch_project(db, project.id) == 0
    assert result.lines[0].part_id is None


# ---------------------------------------------------------------------------
# 4. mpn_norm was left holding the previous raw text's key
# ---------------------------------------------------------------------------


def _import_one(client: TestClient, content: str) -> tuple[int, int]:
    created = client.post("/api/projects", json={"name": "Edit probe"}).json()
    project_id = int(created["project"]["id"])
    imported = client.post(
        f"/api/projects/{project_id}/bom/import", json={"content": content}
    ).json()
    return project_id, int(imported["lines"][0]["id"])


def test_correcting_an_mpn_by_hand_rederives_the_normalised_key(client: TestClient) -> None:
    """The defect: `PUT /bom` copied `mpn_raw` through the plain field loop and
    left `mpn_norm` derived from the *old* text — a row written under a different
    rule than `normalize_mpn(mpn_raw)`, invisible to the matcher while looking
    perfectly correct on screen."""
    project_id, line_id = _import_one(client, "Reference,Value,MPN,Qty\nU1,LM358,AAA111,1\n")

    response = client.put(
        f"/api/projects/{project_id}/bom",
        json={"edits": [{"id": line_id, "mpn_raw": "LM358DR"}]},
    )
    assert response.status_code == 200, response.text
    (line,) = response.json()["lines"]
    assert line["mpn_raw"] == "LM358DR"
    assert line["mpn_norm"] == "lm358dr"


def test_a_corrected_line_matches_the_part_it_now_names(client: TestClient, db: Session) -> None:
    """Both halves of the same defect, in one story: before the fix a corrected
    line re-matched to the part for the MPN the user had just deleted, and a
    corrected typo could never find the part that does exist."""
    wrong = make_part(db, name="Mystery", mpn="AAA111")
    right = make_part(db, name="TI LM358DR", mpn="LM358DR")
    db.commit()

    project_id, line_id = _import_one(client, "Reference,Value,MPN,Qty\nU1,LM358,AAA111,1\n")
    client.put(
        f"/api/projects/{project_id}/bom",
        json={"edits": [{"id": line_id, "mpn_raw": "LM358DR", "part_id": None}]},
    )

    lines = client.get(f"/api/projects/{project_id}/bom").json()["lines"]
    assert lines[0]["mpn_norm"] == "lm358dr"

    from app.services.bom_import import rematch_project as rematch

    assert rematch(db, project_id) == 1
    db.commit()
    refreshed = client.get(f"/api/projects/{project_id}/bom").json()["lines"][0]
    assert refreshed["part_id"] == right.id != wrong.id


def test_clearing_an_mpn_clears_its_normalised_copy(client: TestClient) -> None:
    """The other direction of the same derivation: an empty `mpn_raw` must not
    leave a key behind that the matcher would keep hitting."""
    project_id, line_id = _import_one(client, "Reference,Value,MPN,Qty\nU1,LM358,AAA111,1\n")

    response = client.put(
        f"/api/projects/{project_id}/bom",
        json={"edits": [{"id": line_id, "mpn_raw": None}]},
    )
    (line,) = response.json()["lines"]
    assert line["mpn_raw"] is None
    assert line["mpn_norm"] is None


# ---------------------------------------------------------------------------
# 5. One wide CSV cell raised _csv.Error out of a "never raises" module
# ---------------------------------------------------------------------------


def test_a_field_wider_than_csvs_default_cap_is_imported_not_a_500(client: TestClient) -> None:
    """`csv`'s per-field limit is 131072 characters, a twentieth of the 5,000,000
    the route advertises, so a single wide cell 500'd. The realistic trigger is
    not a wide cell at all: one unbalanced quote makes the whole file tail into a
    single field."""
    project_id = client.post("/api/projects", json={"name": "Wide cell"}).json()["project"]["id"]

    response = client.post(
        f"/api/projects/{project_id}/bom/import",
        json={"content": "Reference,Value,Qty\nR1," + "x" * 200_000 + ",1\n"},
    )
    assert response.status_code == 200, response.text
    (line,) = response.json()["lines"]
    assert line["designators"] == "R1"


def test_an_unbalanced_quote_lands_the_file_with_a_warning(client: TestClient) -> None:
    """A 4000-line export with one stray `"` in a description cell. Before the
    fix this was a 500 on a file whose only defect is one character."""
    rows = [f"R{index},RES 10K 1% 0603,R_0603_1608Metric,1" for index in range(4000)]
    rows[1] = 'R1,"RES 10K 1% 0603,R_0603_1608Metric,1'
    content = "Reference,Description,Footprint,Qty\n" + "\n".join(rows) + "\n"
    project_id = client.post("/api/projects", json={"name": "Stray quote"}).json()["project"]["id"]

    response = client.post(f"/api/projects/{project_id}/bom/import", json={"content": content})
    assert response.status_code == 200, response.text
    assert response.json()["lines"], "the file has to land, however badly quoted"


def test_parse_bom_still_never_raises_when_even_the_raised_cap_is_passed() -> None:
    """The belt-and-braces half of the fix: past `_MAX_FIELD_CHARS` the reader
    degrades to a warning rather than an exception, because `parse_bom` is a
    library function and the route's bound is not its bound."""
    from app.services import bom_import

    huge = "Reference,Value\nR1," + "x" * (bom_import._MAX_FIELD_CHARS + 10) + "\n"
    parsed = bom_import.parse_bom(huge)
    assert any("could not be read" in warning for warning in parsed.all_warnings)


# ---------------------------------------------------------------------------
# 6. A headerless file silently lost its first component
# ---------------------------------------------------------------------------


def test_a_headerless_file_keeps_its_first_component(db: Session) -> None:
    """The fallback used to spend row 1 on being the header, so a two-component
    file landed one line and `R1 / 10k` existed nowhere in the database — with a
    warning that said the header had been guessed and nothing about a dropped
    component. The contract in the docstring is that *every* cell reaches
    `raw_fields_json`."""
    parsed = parse_bom("R1,10k,1\nC1,100nF,1\n")

    assert len(parsed.lines) == 2
    assert parsed.lines[0].raw_fields == {"column_1": "R1", "column_2": "10k", "column_3": "1"}
    assert parsed.lines[0].source_row == 1
    assert parsed.preamble == ()

    project = make_project(db)
    result = import_bom(db, project, parsed)
    assert result.line_count == 2


# ---------------------------------------------------------------------------
# 7. A rival MPN column was dropped with no signal through the API
# ---------------------------------------------------------------------------


def test_a_second_mpn_column_is_reported_not_silently_dropped(client: TestClient) -> None:
    """Header priority picking the first column is defensible. Telling the client
    nothing is not: a user who put the real part number in the second column got a
    confidently matched wrong line and a clean import report."""
    project_id = client.post("/api/projects", json={"name": "Rival column"}).json()["project"]["id"]

    response = client.post(
        f"/api/projects/{project_id}/bom/import",
        json={"content": "Reference,Value,MPN,mpn,Qty\nU1,LM358,AAA111,BBB222,1\n"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert any("names the mpn field" in warning for warning in body["warnings"]), body["warnings"]
    assert body["lines"][0]["mpn_raw"] == "AAA111"


def test_the_documented_alias_priority_is_still_what_wins(db: Session) -> None:
    """The warning must not have changed the resolution rule: `Footprint` beats
    `Package` by alias priority regardless of column order, and only the loser is
    named."""
    parsed = parse_bom("Reference,Package,Footprint\nR1,0603,R_0603_1608Metric\n")

    assert parsed.lines[0].footprint == "R_0603_1608Metric"
    assert any("'Package'" in warning for warning in parsed.warnings)


def test_a_file_with_no_rival_columns_warns_about_nothing(db: Session) -> None:
    """A warning on every ordinary import would train the user to ignore it."""
    parsed = parse_bom("Reference,Value,Footprint,Qty\nR1,10k,R_0603_1608Metric,1\n")
    assert parsed.warnings == ()
