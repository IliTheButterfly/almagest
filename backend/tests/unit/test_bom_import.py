"""The BOM reader, against files shaped like the ones that actually arrive.

`tests/fixtures/bom/*.csv` **are** the ground truth here, hand-verified, for the
same reason the ECIA fixtures are: there is no reference implementation to diff
against, because "a KiCad BOM" is not one format. Every expectation below was
checked against the file by eye rather than against another parser.

The most valuable test here is the one that looks least interesting:
`test_a_semicolon_file_is_not_read_as_one_column` guards the delimiter choice,
which the obvious implementation (count the separators) gets wrong on an ordinary
board, because one grouped decoupling line puts forty commas in a cell.

Nothing here touches a database. Matching lives in
`tests/integration/test_bom_import.py` — including
`test_a_passive_value_is_never_mistaken_for_a_part_number`, which guards the one
place in this feature where a machine could silently allocate the wrong
component — because it is a query.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.bom_import import (
    BomField,
    ParsedBom,
    QtySource,
    parse_bom,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "bom"


def load(name: str) -> ParsedBom:
    return parse_bom((FIXTURES / f"{name}.csv").read_bytes())


@pytest.fixture
def kicad8() -> ParsedBom:
    return load("kicad8_grouped")


# ---------------------------------------------------------------------------
# The modern grouped export
# ---------------------------------------------------------------------------


def test_the_kicad8_grouped_export_maps_every_column(kicad8: ParsedBom) -> None:
    """The default `Export BOM` field set plus two user fields, all recognised
    and nothing left over — the case that has to be effortless."""
    assert kicad8.columns == {
        BomField.DESIGNATORS: "Reference",
        BomField.VALUE: "Value",
        BomField.DATASHEET: "Datasheet",
        BomField.FOOTPRINT: "Footprint",
        BomField.QUANTITY: "Qty",
        BomField.DNP: "DNP",
        BomField.MPN: "MPN",
        BomField.MANUFACTURER: "Manufacturer",
    }
    assert kicad8.unmapped_headers == ()
    assert kicad8.warnings == ()
    assert len(kicad8.lines) == 7


def test_a_grouped_line_counts_its_designators(kicad8: ParsedBom) -> None:
    """`"C1,C2,C3,C4"` with `Qty 4` agrees, so the count wins and no warning is
    raised. Quantities are milli-units like every quantity in this schema."""
    line = kicad8.lines[0]
    assert line.designator_refs == ("C1", "C2", "C3", "C4")
    assert line.qty_per_assembly_milli == 4_000
    assert line.qty_source is QtySource.DESIGNATOR_COUNT
    assert line.declared_qty_milli == 4_000
    assert line.warnings == ()


def test_kicads_tilde_means_no_datasheet(kicad8: ParsedBom) -> None:
    """KiCad writes `~` into an unset field. Storing it verbatim in a typed
    column would make "has a datasheet" true for every line in the file."""
    assert all(line.datasheet is None for line in kicad8.lines)
    # ...and it is still in the archive copy, because that is the archive's job.
    assert kicad8.lines[0].raw_fields["Datasheet"] == "~"


def test_the_dnp_column_marks_the_line_not_fitted(kicad8: ParsedBom) -> None:
    """The line still exists — it is in the file, and it gets fitted next
    revision — so `is_dnp` is what shortage math filters on, not absence."""
    fitted = [line for line in kicad8.lines if not line.is_dnp]
    not_fitted = [line for line in kicad8.lines if line.is_dnp]
    assert [line.designators for line in not_fitted] == ["D1,D2,D3"]
    assert len(fitted) == 6
    # Demand is still computed: a DNP line is a real line with a real quantity,
    # and it is `bom_lines.is_dnp` that zeroes its demand, not this number.
    assert not_fitted[0].qty_per_assembly_milli == 3_000


def test_an_empty_flag_cell_is_not_a_dnp(kicad8: ParsedBom) -> None:
    """The trap in the other direction: reading `""` as truthy would mark every
    line in a file that carries the column at all as do-not-populate."""
    assert kicad8.lines[0].raw_fields.get("DNP") is None
    assert kicad8.lines[0].is_dnp is False


# ---------------------------------------------------------------------------
# The old XSL exports: preamble, and different header spellings
# ---------------------------------------------------------------------------


def test_the_old_xsl_export_is_read_past_its_preamble() -> None:
    """`bom2grouped_csv.xsl` puts `Source:`/`Date:`/`Component Count:` above the
    table. Assuming row 1 is the header makes this file import as five garbage
    lines, which is why the header is searched for rather than assumed."""
    parsed = load("kicad5_bom2grouped")

    assert len(parsed.preamble) == 6
    assert parsed.preamble[0] == ("Source:", "/home/ada/proj/lamp/lamp.sch")
    assert parsed.columns[BomField.DESIGNATORS] == "Ref"
    assert parsed.columns[BomField.QUANTITY] == "Qnty"
    assert [line.designators for line in parsed.lines] == ["C1", "R1, R2", "J1"]


def test_warnings_quote_the_file_row_not_the_line_number() -> None:
    """With a preamble, "line 2" and "row 2" are different rows, and the user is
    looking at the file. `source_row` counts the preamble and the header."""
    parsed = load("kicad5_bom2grouped")
    first = parsed.lines[0]
    assert (first.line_no, first.source_row) == (1, 8)


def test_designators_separated_by_comma_and_space() -> None:
    """`"R1, R2"` is what the XSL templates emit. Splitting on the comma alone
    leaves `" R2"`, which then counts as a designator whose name has a space."""
    parsed = load("kicad5_bom2grouped")
    assert parsed.lines[1].designator_refs == ("R1", "R2")
    assert parsed.lines[1].qty_per_assembly_milli == 2_000


def test_columns_the_schema_has_no_home_for_are_reported_not_dropped() -> None:
    """`Cmp name` and `Vendor` have no typed column. They are still in every
    row's raw fields — the point being that a field this schema does not model
    is not a field the import is allowed to lose."""
    parsed = load("kicad5_bom2grouped")
    assert parsed.unmapped_headers == ("Cmp name", "Vendor")
    assert parsed.lines[0].raw_fields["Cmp name"] == "C"


# ---------------------------------------------------------------------------
# The delimiter and the alias table
# ---------------------------------------------------------------------------


def test_a_semicolon_file_is_not_read_as_one_column() -> None:
    """**The load-bearing delimiter test.** This fixture contains *more commas
    than semicolons*, because one grouped line lists forty decoupling capacitors
    — which is an ordinary board, not a contrived file. So choosing the delimiter
    by counting separator characters reads the whole thing as one comma-delimited
    column with no header, and every line lands with no designators and a
    defaulted quantity. Scoring on *recognised columns* cannot be fooled that
    way, because the comma reading recognises none.
    """
    text = (FIXTURES / "unusual_headers.csv").read_text()
    # The property that breaks the naive implementation, asserted so a later
    # edit to the fixture cannot quietly make this test meaningless.
    assert text.count(",") > text.count(";")

    parsed = load("unusual_headers")
    assert parsed.delimiter == ";"
    assert parsed.lines[0].designators == "R1 R2 R5"
    assert len(parsed.lines[3].designator_refs) == 40


def test_headers_are_matched_by_alias_not_by_spelling() -> None:
    """Not one header in this file is spelled the way KiCad spells it: case,
    punctuation, abbreviation and word spacing all differ."""
    parsed = load("unusual_headers")
    assert parsed.columns == {
        BomField.DESIGNATORS: "RefDes",
        BomField.VALUE: "Val",
        BomField.MPN: "Mfr. Part No",
        BomField.MANUFACTURER: "MFR",
        BomField.FOOTPRINT: "Package",
        BomField.QUANTITY: "QTY",
        BomField.DNP: "Do Not Populate",
    }


@pytest.mark.parametrize(
    ("header", "expected_field"),
    [
        # Deliberately headers the alias table does *not* list literally, so this
        # exercises the de-pluralising step rather than the table. A pluralisation
        # mismatch on the designator column is a whole BOM imported with no
        # designators and every quantity defaulted, silently.
        ("Designators", BomField.DESIGNATORS),
        ("Refs", BomField.DESIGNATORS),
        ("Values", BomField.VALUE),
        ("Footprints", BomField.FOOTPRINT),
    ],
)
def test_a_plural_header_is_the_same_header(header: str, expected_field: BomField) -> None:
    parsed = parse_bom(f"{header},MPN\nR1,LM358DR\n")
    assert parsed.columns[expected_field] == header


def test_footprint_wins_over_package_when_a_file_has_both() -> None:
    """Alias order is the collision rule. `Package` is a real spelling for this
    column *and* a real name for a different one, so a file carrying both must
    not have its footprint read out of the package column."""
    parsed = parse_bom("Reference,Value,Footprint,Package\nR1,10k,R_0603,0603\n")
    assert parsed.columns[BomField.FOOTPRINT] == "Footprint"
    assert parsed.lines[0].footprint == "R_0603"
    assert parsed.unmapped_headers == ("Package",)


def test_an_explicit_mpn_column_wins_over_a_generic_part_number() -> None:
    """`Part Number` could be an internal or a distributor number, so it is
    accepted only when nothing says `MPN` outright."""
    parsed = parse_bom("Reference,Part Number,MPN\nU1,INT-00042,LM358DR\n")
    assert parsed.columns[BomField.MPN] == "MPN"
    assert parsed.lines[0].mpn_raw == "LM358DR"
    assert parsed.lines[0].raw_fields["Part Number"] == "INT-00042"


def test_a_tab_separated_file_is_read_as_one() -> None:
    parsed = parse_bom("Reference\tValue\tQty\nR1\t10k\t1\n")
    assert parsed.delimiter == "\t"
    assert parsed.lines[0].ref_value == "10k"


# ---------------------------------------------------------------------------
# Quantity: the one number that is computed rather than copied
# ---------------------------------------------------------------------------


def test_a_quantity_that_disagrees_with_the_designators_keeps_both_numbers() -> None:
    """**Never silently resolved.** `"R1,R2,R3"` with `Qty 4` is evidence about
    the export — a truncated designator cell, an unexpanded range, a hand-edit
    — so both numbers survive and the row says so in words a reviewer will read
    three weeks later. The larger is used, because over-reserving is visible in
    the shortage report and released with one action while under-reserving is
    discovered at the bench with half a board populated."""
    line = load("qty_disagreement").lines[0]

    assert line.qty_source is QtySource.DISAGREEMENT_MAX
    assert line.qty_per_assembly_milli == 4_000
    assert line.declared_qty_milli == 4_000
    assert len(line.designator_refs) == 3
    assert line.note is not None
    assert "quantity column says 4 but 3 designators listed" in line.note


def test_an_expanded_range_does_not_manufacture_a_disagreement() -> None:
    """`R10-R13` with `Qty 4` is a consistent line. A reader that counted one
    designator would warn on every ranged line in the file and teach the user to
    ignore the warning — which is the only thing that makes it worth raising."""
    line = load("qty_disagreement").lines[2]
    assert line.designator_refs == ("R10", "R11", "R12", "R13")
    assert line.qty_source is QtySource.DESIGNATOR_COUNT
    assert line.warnings == ()


def test_a_missing_quantity_cell_falls_back_to_the_designators() -> None:
    line = load("qty_disagreement").lines[3]
    assert line.declared_qty_milli is None
    assert (line.qty_per_assembly_milli, line.qty_source) == (1_000, QtySource.DESIGNATOR_COUNT)


def test_a_line_with_neither_still_gets_a_quantity() -> None:
    """`qty_per_assembly_milli` is NOT NULL and a zero-demand line is
    indistinguishable from a DNP, so the fallback is one per assembly — said out
    loud, because it is an assumption and not a reading."""
    line = parse_bom("MPN,Manufacturer\nLM358DR,TI\n").lines[0]
    assert (line.qty_per_assembly_milli, line.qty_source) == (1_000, QtySource.FALLBACK)
    assert "assumed one per assembly" in " ".join(line.warnings)


def test_a_fractional_quantity_survives_as_milli_units() -> None:
    """Half a metre of wire is a legal BOM line. `Decimal`, not `float`:
    `int(0.5 * 1000)` is only reliably 500 by luck."""
    line = parse_bom("Reference,Value,Qty\n,Wire 22AWG,0.5\n").lines[0]
    assert line.qty_per_assembly_milli == 500


def test_an_ambiguous_decimal_quantity_is_refused_rather_than_guessed() -> None:
    """`1,5` is one and a half in half of Europe and fifteen hundred in the
    other half. Guessing wrong is a thousandfold error in a demand figure."""
    line = parse_bom('Reference,Value,Qty\nR1,10k,"1,5"\n').lines[0]
    assert line.declared_qty_milli is None
    assert "ambiguous decimal" in " ".join(line.warnings)
    # The line still lands, with the number it *can* justify.
    assert line.qty_per_assembly_milli == 1_000


@pytest.mark.parametrize("cell", ["four", "0", "-3"])
def test_an_unusable_quantity_is_ignored_not_fatal(cell: str) -> None:
    line = parse_bom(f"Reference,Value,Qty\nR1,10k,{cell}\n").lines[0]
    assert line.declared_qty_milli is None
    assert line.qty_per_assembly_milli == 1_000
    assert line.warnings != ()


# ---------------------------------------------------------------------------
# Designator lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("R1,R2,R5", ("R1", "R2", "R5")),
        ("R1 R2 R5", ("R1", "R2", "R5")),
        ("R1;R2", ("R1", "R2")),
        ("R1-R3", ("R1", "R2", "R3")),
        ("R1..R3", ("R1", "R2", "R3")),
        # A right-hand prefix is optional: `R1-3` is written by hand constantly.
        ("R1-3", ("R1", "R2", "R3")),
        # Zero padding is preserved — a designator is a string, and `R01` is not
        # `R1` on the silkscreen.
        ("R01-R03", ("R01", "R02", "R03")),
    ],
)
def test_designator_notations(cell: str, expected: tuple[str, ...]) -> None:
    # Quoted, because a designator cell holding commas is exactly why an exporter
    # quotes it — and an unquoted one is a different bug (a wide row), tested
    # separately.
    parsed = parse_bom(f'Reference,Value\n"{cell}",10k\n')
    assert parsed.lines[0].designator_refs == expected


@pytest.mark.parametrize("cell", ["R5-R1", "R1-R9999", "R1-C4"])
def test_a_range_that_is_not_one_is_kept_verbatim(cell: str) -> None:
    """Backwards, absurdly wide, or across two prefixes. Expanding `R1-R9999`
    would turn one bad cell into a demand for ten thousand parts; keeping the
    token loses nothing, because the text is still on the row."""
    line = parse_bom(f"Reference,Value\n{cell},10k\n").lines[0]
    assert line.designator_refs == (cell,)
    assert line.warnings != ()


def test_repeated_designators_are_kept_and_flagged() -> None:
    """Deduplicating would hide a schematic error inside a quantity that
    silently no longer matches the board."""
    line = parse_bom('Reference,Value\n"R1,R1,R2",10k\n').lines[0]
    assert line.designator_refs == ("R1", "R1", "R2")
    assert line.qty_per_assembly_milli == 3_000
    assert "designators repeated: R1" in " ".join(line.warnings)


def test_designators_are_truncated_to_the_column_but_not_lost() -> None:
    """A hundred-capacitor decoupling line really does overflow the column.
    SQLite would not complain, which is exactly why this is done in code — and
    it is safe only because the untruncated text stays in the raw fields."""
    cell = ",".join(f"C{index}" for index in range(1, 300))
    line = parse_bom(f'Reference,Value\n"{cell}",100nF\n').lines[0]

    assert line.designators is not None
    assert len(line.designators) == 1024
    assert line.raw_fields["Reference"] == cell
    assert len(line.designator_refs) == 299
    assert "truncated" in " ".join(line.warnings)


# ---------------------------------------------------------------------------
# The value parser: telling a value from a part number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("designator", "value", "expected"),
    [
        ("R1", "4k7", 4700.0),
        ("R1", "220R", 220.0),
        # A 0 Ω jumper is a real, commonly stocked component.
        ("R1", "0", 0.0),
        ("C1", "100nF", 1e-7),
        ("C1", "2.2µF", 2.2e-6),
        ("L1", "10µH", 1e-5),
    ],
)
def test_a_value_is_parsed_against_the_quantity_its_designator_implies(
    designator: str, value: str, expected: float
) -> None:
    """The designator prefix is the only thing in a BOM that says what physical
    quantity the value column expresses. `1M` under `R` is a megohm; under `C`
    it is impossible."""
    line = parse_bom(f"Reference,Value\n{designator},{value}\n").lines[0]
    assert line.value is not None
    assert line.value.value_nominal == pytest.approx(expected)


@pytest.mark.parametrize("designator", ["U1", "J1", "D1", "Q1"])
def test_no_value_is_parsed_for_a_designator_class_whose_value_is_a_name(
    designator: str,
) -> None:
    """`U1` = `1M` is not a resistance and never was. Parsing it anyway is how a
    BOM line acquires a fabricated parameter that gets searched against."""
    line = parse_bom(f"Reference,Value\n{designator},1M\n").lines[0]
    assert line.value is None
    # `None` reason distinguishes "no parse attempted" from "parse refused",
    # which is what the part-number fallback keys off.
    assert line.value_parse_error is None


def test_a_refused_value_is_reported_as_refused_not_as_absent() -> None:
    """`1M` is an absurd capacitance, and the parser refusing it is a curation
    item, not an error path. The distinction from the case above is load-bearing:
    only *this* one means "the cell is not a value"."""
    line = parse_bom("Reference,Value\nC1,1M\n").lines[0]
    assert line.value is None
    assert line.value_parse_error == "implausible"


def test_a_mixed_designator_line_parses_no_value() -> None:
    """A grouped line covering `R1,C2` is a malformed export, and picking either
    prefix parses the value against a quantity half the line contradicts."""
    line = parse_bom('Reference,Value\n"R1,C2",10k\n').lines[0]
    assert line.value is None


# ---------------------------------------------------------------------------
# The never-fails contract
# ---------------------------------------------------------------------------


def test_an_empty_file_is_an_empty_import_not_an_error() -> None:
    """A user who exported the wrong thing gets told so. Raising would make the
    most common mistake in the workflow look like a broken feature."""
    parsed = load("empty")
    assert parsed.lines == ()
    assert parsed.warnings == ("file is empty",)


def test_a_header_with_no_rows_still_reports_its_columns() -> None:
    """Distinct from empty: the columns are known, so the user can see the
    export was configured correctly and simply had nothing in it."""
    parsed = load("header_only")
    assert parsed.lines == ()
    assert BomField.DESIGNATORS in parsed.columns


def test_a_file_with_no_recognisable_header_still_lands_every_cell() -> None:
    """The worst case has to be a worklist, not a refusal. Every cell reaches
    the raw fields, so the file is in the system and a human can fix it — which
    is strictly better than an error message and a file left on a laptop.

    **Including the first row's cells.** This test used to assert one line for a
    two-component file: the fallback spent row 1 on being the header, so `R1 /
    10k` existed nowhere in the database and no warning said a component had been
    dropped. See `test_phase2_review_findings`.
    """
    parsed = parse_bom("R1,10k,0603\nR2,4k7,0603\n")

    assert parsed.columns == {}
    assert "no recognisable header row" in " ".join(parsed.warnings)
    assert len(parsed.lines) == 2
    assert parsed.lines[0].raw_fields == {"column_1": "R1", "column_2": "10k", "column_3": "0603"}
    assert parsed.lines[1].raw_fields == {"column_1": "R2", "column_2": "4k7", "column_3": "0603"}


def test_a_row_wider_than_its_header_keeps_the_extra_cells() -> None:
    """A malformed export's extra cells are the evidence of what went wrong."""
    line = parse_bom("Reference,Value\nR1,10k,extra,more\n").lines[0]
    assert line.raw_fields["column_3"] == "extra"
    assert line.raw_fields["column_4"] == "more"
    assert "4 cells for 2 columns" in " ".join(line.warnings)


def test_duplicate_headers_both_survive() -> None:
    """One would otherwise silently overwrite the other in the archive copy."""
    line = parse_bom("Reference,Value,Value\nR1,10k,4k7\n").lines[0]
    assert line.raw_fields["Value"] == "10k"
    assert line.raw_fields["Value__3"] == "4k7"


def test_blank_rows_are_layout_and_consume_no_line_number() -> None:
    """The XSL templates emit one between the preamble and the table, and a
    spreadsheet round-trip leaves them everywhere. `line_no` is what the user saw
    in KiCad, so a spacer must not shift it."""
    parsed = parse_bom("Reference,Value\n\nR1,10k\n\nR2,4k7\n")
    assert [(line.line_no, line.designators) for line in parsed.lines] == [(1, "R1"), (2, "R2")]


def test_a_row_naming_nothing_identifiable_is_kept_and_flagged() -> None:
    """An import that silently dropped rows would be worse than one that lands a
    row saying it cannot tell what the row is."""
    line = parse_bom("Reference,Value,Qty\n,,7\n").lines[0]
    assert line.qty_per_assembly_milli == 7_000
    assert "names no designator, value or part number" in " ".join(line.warnings)


# ---------------------------------------------------------------------------
# What Windows actually sends
# ---------------------------------------------------------------------------


def test_a_utf8_bom_marker_and_crlf_do_not_corrupt_the_first_column() -> None:
    """Both are what actually arrives from a Windows export. A stray `\\ufeff`
    glued to `Reference` unmaps the designator column silently — the whole BOM
    imports, with no designators and every quantity defaulted."""
    parsed = load("windows_crlf_bom")

    assert parsed.columns[BomField.DESIGNATORS] == "Reference"
    assert parsed.lines[0].designator_refs == ("C1",)
    assert parsed.lines[0].raw_fields["Reference"] == "C1"
    assert parsed.lines[0].mpn_norm == "grm21br61a225ka01l"


def test_a_bom_marker_is_stripped_from_text_input_too() -> None:
    """`Path.read_text()` keeps the marker where `read_bytes()` + `utf-8-sig`
    strips it, so a caller that reads the file as text must not get a different
    answer from one that reads bytes."""
    text = (FIXTURES / "windows_crlf_bom.csv").read_text(encoding="utf-8")
    assert text.startswith("﻿")  # the trap this test exists for
    assert parse_bom(text).columns == load("windows_crlf_bom").columns


def test_a_cp1252_export_is_read_rather_than_refused() -> None:
    """The same machines that send a BOM marker also send cp1252 when they are
    not being helpful, and `2.2µF` is exactly where it shows up."""
    parsed = parse_bom("Reference,Value\nC1,2.2µF\n".encode("cp1252"))
    assert parsed.lines[0].ref_value == "2.2µF"


def test_undecodable_bytes_are_replaced_not_raised() -> None:
    """A mangled character is a review item. A `UnicodeDecodeError` is a file the
    user cannot import at all, which is the one outcome this module forbids."""
    # 0x81 is undefined in cp1252 and a bare continuation byte in UTF-8, so it
    # defeats both of the strict decodes.
    parsed = parse_bom(b"Reference,Value\nR1,10\x81k\n")
    assert len(parsed.lines) == 1
    assert "undecodable bytes" in " ".join(parsed.warnings)


# ---------------------------------------------------------------------------
# The archive copy
# ---------------------------------------------------------------------------


def test_every_populated_cell_reaches_the_raw_fields(kicad8: ParsedBom) -> None:
    """`raw_fields_json` is the reason a better matcher written next year can be
    re-run: it only works if the original text survived, mapped columns
    included. Empty cells are omitted — a CSV cannot express the difference
    between empty and absent, so inventing one would be a fiction."""
    line = kicad8.lines[4]
    assert line.raw_fields == {
        "Reference": "U1",
        "Value": "LM358",
        "Datasheet": "~",
        "Footprint": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        "Qty": "1",
        "MPN": "LM358DR",
        "Manufacturer": "Texas Instruments",
    }


def test_mpn_norm_is_a_copy_of_mpn_raw_and_nothing_else(kicad8: ParsedBom) -> None:
    """The column's contract. The `Value`-as-part-number fallback is allowed to
    *look up* a value cell but never to write it here, because a `mpn_norm` the
    file never stated would make the line claim a part number it does not have.
    """
    ic = kicad8.lines[5]
    assert (ic.ref_value, ic.mpn_raw, ic.mpn_norm) == ("ATmega328P-AU", None, None)
    matched = kicad8.lines[4]
    assert (matched.mpn_raw, matched.mpn_norm) == ("LM358DR", "lm358dr")


def test_all_warnings_prefixes_each_row_warning_with_its_row() -> None:
    parsed = load("qty_disagreement")
    assert any(
        warning.startswith("row 2: quantity column says 4") for warning in parsed.all_warnings
    )
