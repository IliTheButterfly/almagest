"""Landing a parsed BOM, and matching it to the catalogue.

Split from `tests/unit/test_bom_import.py` because everything here is a query.
Two things are under test and only one of them is about SQL:

* **the import lands, always.** A BOM with nothing matchable in it still
  produces one `bom_lines` row per file row, because `part_id` is nullable
  precisely so curation can be deferred — the intake-queue argument.
* **matching is conservative.** `test_a_passive_value_is_never_mistaken_for_a_
  part_number` is the load-bearing test in this file: it is the one place where a
  machine could silently attach the wrong component to a build, which is worse
  than a line that says "unknown".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.projects import BomLine, Project
from app.services.bom_import import ParsedBom, import_bom, parse_bom, rematch_project
from tests.factories import make_part, make_project

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "bom"


def load(name: str) -> ParsedBom:
    return parse_bom((FIXTURES / f"{name}.csv").read_bytes())


@pytest.fixture
def project(db: Session) -> Project:
    return make_project(db, name="Lamp controller", revision="rev B")


def lines_of(db: Session, project: Project) -> list[BomLine]:
    return list(
        db.execute(
            select(BomLine).where(BomLine.project_id == project.id).order_by(BomLine.line_no)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# The import lands
# ---------------------------------------------------------------------------


def test_an_import_lands_every_line_even_when_nothing_matches(
    db: Session, project: Project
) -> None:
    """**The point of the whole feature.** An empty catalogue is the normal state
    on the first import, and a BOM that could not be stored until every line
    resolved would send the user back to a spreadsheet."""
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert result.line_count == 7
    assert result.matched_count == 0
    assert result.unmatched_count == 7
    rows = lines_of(db, project)
    assert [row.line_no for row in rows] == [1, 2, 3, 4, 5, 6, 7]
    assert all(row.part_id is None for row in rows)
    assert rows[0].designators == "C1,C2,C3,C4"


def test_the_imported_columns_are_stored_where_the_schema_has_a_home_for_them(
    db: Session, project: Project
) -> None:
    import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    capacitor = lines_of(db, project)[0]
    assert capacitor.ref_value == "100nF"
    assert capacitor.footprint == "Capacitor_SMD:C_0603_1608Metric"
    assert capacitor.mpn_raw == "CL10B104KB8NNNC"
    assert capacitor.mpn_norm == "cl10b104kb8nnnc"
    assert capacitor.manufacturer_raw == "Samsung"
    assert capacitor.qty_per_assembly_milli == 4_000
    assert capacitor.is_dnp is False


def test_the_whole_source_row_is_stored_as_sorted_json(db: Session, project: Project) -> None:
    """Sorted keys so re-importing the same file produces byte-identical JSON —
    which is what makes a diff between two imports readable instead of a
    reshuffle. Unmapped columns are in here and that is the entire point: a
    field this schema does not model is not a field the import may lose."""
    import_bom(db, project, load("kicad5_bom2grouped"))
    db.commit()

    row = lines_of(db, project)[0]
    assert row.raw_fields_json is not None
    stored = json.loads(row.raw_fields_json)
    assert list(stored) == sorted(stored)
    assert stored["Cmp name"] == "C"


def test_a_row_warning_is_written_to_the_line_it_came_from(db: Session, project: Project) -> None:
    """A quantity disagreement is not an import-log entry, it is a property of
    the row — and whoever opens this BOM in three weeks will not read the log."""
    import_bom(db, project, load("qty_disagreement"))
    db.commit()

    rows = lines_of(db, project)
    assert rows[0].note is not None
    assert "quantity column says 4 but 3 designators listed" in rows[0].note
    assert rows[0].qty_per_assembly_milli == 4_000
    # ...and a line with nothing to say says nothing.
    assert rows[1].note is None


def test_a_dnp_line_lands_as_a_line(db: Session, project: Project) -> None:
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert result.dnp_count == 1
    assert [row.line_no for row in lines_of(db, project) if row.is_dnp] == [7]


def test_an_empty_file_imports_nothing_and_says_why(db: Session, project: Project) -> None:
    result = import_bom(db, project, load("empty"))
    db.commit()

    assert result.line_count == 0
    assert result.warnings == ("file is empty",)
    assert lines_of(db, project) == []


def test_a_second_import_continues_the_line_numbering(db: Session, project: Project) -> None:
    """Two sheets into one project must not produce two "line 1"s. `line_no` is
    not unique per project — a malformed file has to be able to land — so
    nothing in the schema would have caught this."""
    import_bom(db, project, load("qty_disagreement"))
    second = import_bom(db, project, load("windows_crlf_bom"))
    db.commit()

    assert [row.line_no for row in lines_of(db, project)] == [1, 2, 3, 4, 5, 6, 7]
    assert any("numbered from 5" in warning for warning in second.warnings)


# ---------------------------------------------------------------------------
# Matching: exact normalised MPN, and nothing looser
# ---------------------------------------------------------------------------


def test_an_exact_mpn_hit_sets_the_part_but_never_confirms_it(
    db: Session, project: Project
) -> None:
    """`is_match_confirmed` stays false. An exact normalised equality is strong
    evidence and still not a human's agreement, and keeping those apart is what
    the column is for — the same rule that forbids auto-accepting an OCR'd part
    number."""
    part = make_part(db, name="LM358 opamp", mpn="LM358DR")
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    matched = [row for row in lines_of(db, project) if row.part_id is not None]
    assert [row.part_id for row in matched] == [part.id]
    assert result.matched_count == 1
    assert all(row.is_match_confirmed is False for row in matched)


def test_a_differently_punctuated_mpn_matches_through_the_shared_normaliser(
    db: Session, project: Project
) -> None:
    """`normalize_mpn` is the single definition of `parts.mpn_norm`. Writing a
    second rule here would produce lines that look correct and are invisible to
    the resolver's bare-MPN step, so this is really a test that no second rule
    exists."""
    part = make_part(db, mpn="lm-358.dr")
    import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert lines_of(db, project)[4].part_id == part.id


def test_a_near_miss_is_left_unmatched(db: Session, project: Project) -> None:
    """`LM358` is not `LM358DR` — different package, different reel, possibly
    different pinout family. A plausible-but-wrong match allocates the wrong
    component to a build; "unknown" costs one review item."""
    make_part(db, mpn="LM358")
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert result.matched_count == 0
    assert lines_of(db, project)[4].part_id is None


def test_two_parts_sharing_an_mpn_leave_the_line_unmatched_and_are_reported(
    db: Session, project: Project
) -> None:
    """Two rows sharing an `mpn_norm` differ by manufacturer, and choosing
    between them is a curation decision. Reported by key so the UI can say which
    line needs the decision."""
    make_part(db, name="TI part", mpn="LM358DR")
    make_part(db, name="Onsemi part", mpn="LM358DR")
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert result.matched_count == 0
    assert result.ambiguous_keys == ("lm358dr",)
    assert lines_of(db, project)[4].part_id is None


def test_an_inactive_part_is_not_auto_matched(db: Session, project: Project) -> None:
    """A retired part is the wrong thing to allocate a new build from, and
    leaving the line unmatched puts that decision in front of the user rather
    than quietly reviving it."""
    make_part(db, mpn="LM358DR", is_active=False)
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert result.matched_count == 0


# ---------------------------------------------------------------------------
# The Value column: a part number for an IC, never for a passive
# ---------------------------------------------------------------------------


def test_a_passive_value_is_never_mistaken_for_a_part_number(db: Session, project: Project) -> None:
    """**The load-bearing test in this file.** `normalize_mpn("10k")` is `"10k"`,
    a perfectly good lookup key, and somebody's stock part really is named `10K`.
    Matching a `R1,R2 / 10k` line to it puts a resistor of unknown value —
    unknown *tolerance*, unknown *power rating* — into a build and calls it
    identified.

    The only thing preventing it is the value parser recognising `10k` as ten
    kilohms *because the designators say `R`*. So this test is the whole reason
    `elec-value-parser` is a dependency of this module.
    """
    make_part(db, name="Mystery 10K", mpn="10K")
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    resistors = lines_of(db, project)[2]
    assert resistors.designators == "R1,R2"
    assert resistors.ref_value == "10k"
    assert resistors.part_id is None
    assert result.matched_count == 0


def test_a_zero_ohm_jumper_is_a_value_not_a_part_number(db: Session, project: Project) -> None:
    """`0` is a real, commonly stocked component and a real value. The guard has
    to survive the degenerate case, because `normalize_mpn("0")` is `"0"` and
    something in a big catalogue will match it."""
    make_part(db, name="Suspicious part 0", mpn="0")
    import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert lines_of(db, project)[3].part_id is None


def test_an_ic_value_is_used_as_a_part_number_when_no_mpn_column_filled_it(
    db: Session, project: Project
) -> None:
    """The most common shape of a hobby BOM: no MPN column, the part number in
    `Value`. Refusing to look there would leave every IC unmatched — and the
    lookup is still an exact normalised equality, which is the same strength of
    evidence as an MPN column, just from a weaker-labelled source."""
    part = make_part(db, name="ATmega328P", mpn="ATMEGA328P-AU")
    result = import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    row = lines_of(db, project)[5]
    assert row.ref_value == "ATmega328P-AU"
    assert row.part_id == part.id
    assert result.matched_count == 1
    # The column's contract survives the match: `mpn_norm` is a copy of
    # `mpn_raw`, so the line never claims a part number the file did not state.
    assert (row.mpn_raw, row.mpn_norm) == (None, None)


def test_a_part_number_typed_into_a_resistors_value_cell_is_still_matched(
    db: Session, project: Project
) -> None:
    """The other half of the same guard, and the reason it is the *parse* and not
    the designator prefix. Somebody typed the part number into the `Value` field
    of `R7`; the grammar finds no number in `RC0603FR-0710KL` at all and refuses
    it on syntax, which is precisely the evidence that it is a part number. A
    prefix rule would leave every line like this unmatched forever."""
    part = make_part(db, name="10k 1% 0603", mpn="RC0603FR-0710KL")
    import_bom(db, project, parse_bom("Reference,Value\nR7,RC0603FR-0710KL\n"))
    db.commit()

    assert lines_of(db, project)[0].part_id == part.id


def test_an_implausible_value_is_still_a_value_not_a_part_number(
    db: Session, project: Project
) -> None:
    """`1M` under `C` is refused as one megafarad, and that refusal means the
    cell was read as a quantity and rejected on physics — a bad value, not a part
    number. Treating every refusal alike would make this a match."""
    make_part(db, name="Suspicious part 1M", mpn="1M")
    import_bom(db, project, parse_bom("Reference,Value\nC9,1M\n"))
    db.commit()

    assert lines_of(db, project)[0].part_id is None


def test_the_row_says_when_the_match_came_from_the_value_column(
    db: Session, project: Project
) -> None:
    """A reviewer confirming this match needs to know it came off the `Value`
    cell rather than a part-number column — a weaker claim, even though the
    lookup itself was exact."""
    make_part(db, mpn="ATMEGA328P-AU")
    import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    row = lines_of(db, project)[5]
    assert row.note is not None
    assert "matched on the Value column" in row.note


def test_an_mpn_column_hit_is_not_annotated(db: Session, project: Project) -> None:
    """The counterpart: the ordinary case must not add noise to every note."""
    make_part(db, mpn="LM358DR")
    import_bom(db, project, load("kicad8_grouped"))
    db.commit()

    assert lines_of(db, project)[4].note is None


def test_matching_can_be_skipped(db: Session, project: Project) -> None:
    """`match=False` exists so a caller can land a file now and match later —
    the same deferral the nullable `part_id` is there to allow."""
    make_part(db, mpn="LM358DR")
    result = import_bom(db, project, load("kicad8_grouped"), match=False)
    db.commit()

    assert result.matched_count == 0
    assert all(row.part_id is None for row in lines_of(db, project))


# ---------------------------------------------------------------------------
# Re-running the matcher later
# ---------------------------------------------------------------------------


def test_rematch_picks_up_a_part_created_after_the_import(db: Session, project: Project) -> None:
    """Why every imported field is kept verbatim: parts created since the import
    — by a scan, by a curation pass — make previously unmatchable lines
    matchable, and this is the pass that notices."""
    import_bom(db, project, load("kicad8_grouped"))
    db.commit()
    assert all(row.part_id is None for row in lines_of(db, project))

    part = make_part(db, mpn="LM358DR")
    assert rematch_project(db, project.id) == 1
    db.commit()

    assert lines_of(db, project)[4].part_id == part.id


def test_rematch_reconstructs_the_same_value_guard(db: Session, project: Project) -> None:
    """The candidate rule is rebuilt from the stored columns rather than from a
    `ParsedBom` that no longer exists. If the two rules could differ, a rematch
    would happily make the passive-value match that the import refused."""
    import_bom(db, project, load("kicad8_grouped"))
    make_part(db, name="Mystery 10K", mpn="10K")
    db.commit()

    assert rematch_project(db, project.id) == 0
    assert lines_of(db, project)[2].part_id is None


def test_rematch_never_unmatches_an_already_matched_line(db: Session, project: Project) -> None:
    """It reads only `part_id IS NULL` — the `ix_bom_lines_unmatched` worklist —
    so a line matched earlier keeps its part even once the key it matched on has
    become ambiguous. Re-deciding a settled line is not this function's job."""
    make_part(db, name="TI part", mpn="LM358DR")
    import_bom(db, project, load("kicad8_grouped"))
    db.commit()
    matched_id = lines_of(db, project)[4].part_id
    assert matched_id is not None

    make_part(db, name="Onsemi part", mpn="LM358DR")
    db.commit()
    assert rematch_project(db, project.id) == 0
    assert lines_of(db, project)[4].part_id == matched_id


def test_rematch_matches_the_semicolon_files_regulator(db: Session, project: Project) -> None:
    """An end-to-end pass over the least KiCad-shaped fixture, so the stored
    columns a rematch depends on are proven to survive a file whose headers were
    all spelled differently."""
    import_bom(db, project, load("unusual_headers"))
    db.commit()
    part = make_part(db, name="7805", mpn="LM7805CT")

    assert rematch_project(db, project.id) == 1
    db.commit()
    assert lines_of(db, project)[1].part_id == part.id
