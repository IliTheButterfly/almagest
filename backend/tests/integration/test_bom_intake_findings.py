"""Regressions for the ten defects adversarial review found in the BOM-intake batch.

Every one was reproduced before it was fixed, and every one was **green code** —
the branch's own 1585-test suite passed with all of them in it. They fall into two
families, and the split is the interesting part of this file.

**Five are a deterministic component inventing a fact.** No model was involved in
any of them; `requirements.interpret` is still wired to nothing but its own unit
test. What produced the wrong answers was a tokeniser doing value-grammar work
*before* the curated-spelling pass it is documented to run after, and an importer
believing a column because it had a familiar name. `10k 0603 1% resistor` came out
with **zero** filters and offered a 220 Ω 1206 resistor as its rank-1 *exact*
match, at `provenance: deterministic, confidence: 1.0`; `10k resistor 1%` came out
carrying the part number `resistor ±1%`, which appears nowhere in the input, and
matched a 220 Ω part on it. A model is not required to invent a plausible part —
a parser with its passes in the wrong order will do it, and it will label the
result deterministic while doing so.

**Three are a machine's ranking or column guess being recorded as a human's
decision, or a sentence claiming more than the SQL proved.** "Accept all" wrote the
rank-1 candidate's `part_id` for every line, which `PUT .../bom` records as
`is_match_confirmed = True` — so one click marked a substitute nobody had looked
at, and parts the user does not own, as "a human agreed". `range_overlap` told the
user the offered value "falls inside" what they asked for while the predicate only
proved an overlap.

**And two are the plain kind:** a bound that did not bind, and a whole panel whose
every write was a 422 because its tests stubbed `fetch` instead of the validator.

The one thing worth carrying forward: in eight of the ten the *loudest* signal was
absent or actively misleading. A wrongly chosen header row produced two warnings
about malformed-export noise and none about the header; a UTF-16 export imported
with `warnings: []`; a stray quote that ate 3 993 components reported a footprint
truncation. Silence is the failure mode this codebase's own docstrings keep naming,
and it is still the one that ships.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.catalog import PartCategory
from app.models.enums import SubstitutionDirection, ValueType
from app.models.storage import Location
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services import parameters
from app.services.bom_import import parse_bom
from app.services.requirements.matching import RequirementInput, suggest_batch
from app.services.requirements.parser import DeterministicRequirementParser
from app.services.requirements.vocabulary import load_vocabulary
from tests.factories import (
    make_bom_line,
    make_location,
    make_lot,
    make_part,
    make_project,
    seed_vocabulary,
)
from tests.integration.test_suggestions import _template, make_capacitor

# ---------------------------------------------------------------------------
# Fixtures — templates and categories only, so two rows differing in one way
# are the whole catalogue
# ---------------------------------------------------------------------------


@pytest.fixture
def catalogue(db: Session) -> Session:
    seed_categories(db)
    seed_parameter_templates(db)
    db.commit()
    return db


@pytest.fixture
def bin_(catalogue: Session) -> Location:
    return make_location(catalogue, "Findings bin")


@pytest.fixture
def parser() -> DeterministicRequirementParser:
    return DeterministicRequirementParser(seed_vocabulary())


def make_resistor(
    db: Session,
    mpn: str,
    *,
    resistance: str,
    package: str,
    description: str | None = None,
    location: Location | None = None,
    qty_milli: int = 0,
) -> None:
    """A resistor with a resistance and a package, through `services.parameters`.

    Never a hand-built `parameter_value`: every numeric row needs
    `value_min`/`value_max` or it is silently invisible to every range query.
    """
    category_id = int(
        db.execute(
            PartCategory.__table__.select()
            .with_only_columns(PartCategory.id)
            .where(PartCategory.slug == "resistor")
        ).scalar_one()
    )
    part = make_part(db, mpn, mpn=mpn, category_id=category_id, description=description)
    parameters.set_numeric(db, part, _template(db, "resistance"), resistance)
    parameters.set_choice(db, part, _template(db, "package"), package)
    if location is not None and qty_milli:
        make_lot(db, part, location, qty_milli=qty_milli)
    db.flush()


def _filters(text: str, parser: DeterministicRequirementParser) -> dict[str, str]:
    return {item.template: item.value for item in parser.parse(text).filters}


def _mpns(candidates: object) -> list[str | None]:
    assert isinstance(candidates, tuple)
    return [candidate.part.mpn for candidate in candidates]


def suggest(db: Session, text: str) -> object:
    return suggest_batch(db, [RequirementInput(text=text)])[0]


# ---------------------------------------------------------------------------
# 1. A `%` token swallowed the package, and both predicates were dropped
# ---------------------------------------------------------------------------


def test_a_tolerance_never_swallows_a_curated_spelling(
    parser: DeterministicRequirementParser,
) -> None:
    """The defect: `10k 0603 1% resistor` parsed to **zero filters**.

    `_tokenise` fused a bare `N%` onto whatever token preceded it, and it ran
    *before* `_consume_vocabulary` — inverting the one ordering the parser's own
    docstring calls load-bearing. `0603` became the token `0603 ±1%`, which is not
    a package spelling, so `_take_choice` never saw it; it reads cleanly as 603 Ω,
    contradicts `10k`, and `without_contradictions` dropped **both**. The line then
    searched on `category=resistor` alone, with `confidence: 1.0` and a badge
    reading "100% deterministic".

    Asserted across word orders, because the file's own corpus only ever carried
    one of them: whichever order the user types, the requirement is the same.
    """
    for text in ("10k 0603 1% resistor", "3x 10k 1% 0603 resistor", "10k 1% 0603 resistor"):
        assert _filters(text, parser) == {
            "resistance": "10k ±1%",
            "package": "0603_1608",
        }, text
        assert not parser.parse(text).rejections, text

    # And the same shape one template over, where the failure *widened* the
    # requirement instead of emptying it: the package filter simply went missing,
    # so a 1206 part came back at distance 0.0 for a line that said 0603.
    assert _filters("100nF 0603 10% X7R", parser) == _filters("100nF 10% 0603 X7R", parser)
    assert _filters("100nF 0603 10% X7R", parser)["package"] == "0603_1608"


def test_a_dropped_package_filter_cannot_offer_the_wrong_part_as_an_exact_match(
    catalogue: Session, bin_: Location
) -> None:
    """The consequence, end to end: the rank-1 exact match was a 220 Ω 1206 part.

    Both parts are resistors, so `category=resistor` — all the requirement had
    left — matched both, and the 220 Ω one won on stock. This is the failure
    `CLAUDE.md` means by "a plausible substitute with the wrong voltage rating is a
    field failure", arrived at with no model anywhere near it.
    """
    make_resistor(
        catalogue,
        "WRONG-220R-1206",
        resistance="220R",
        package="1206",
        location=bin_,
        qty_milli=500_000,
    )
    make_resistor(
        catalogue,
        "RIGHT-10K-0603",
        resistance="10k",
        package="0603",
        location=bin_,
        qty_milli=1_000,
    )
    catalogue.commit()

    answer = suggest(catalogue, "10k 0603 1% resistor")

    assert _mpns(answer.in_stock) == ["RIGHT-10K-0603"]  # type: ignore[attr-defined]
    assert _mpns(answer.not_stocked) == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 2. The same fusion invented a part number the description never contained
# ---------------------------------------------------------------------------


def test_a_tolerance_never_turns_a_word_into_a_part_number(
    parser: DeterministicRequirementParser,
) -> None:
    """The defect: `10k resistor 1%` came out with `mpn = 'resistor ±1%'`.

    `resistor` is a category spelling, but the fusion had already turned it into
    `resistor ±1%`, which is not one. It fell through to `_read_values`, `_HAS_DIGIT`
    passed on the `1`, and `looks_like_a_part_number` said yes (`normalize_mpn` →
    `resistor1`). That string became `SearchQuery.text`, so the line was matched on
    a part number that exists nowhere in the input — labelled deterministic, at
    confidence 1.0.

    The fix has two gates and this pins the second: a tolerance re-attaches only
    onto a token `reads_as_a_quantity` accepts.
    """
    requirement = parser.parse("10k resistor 1%")

    assert (requirement.mpn, requirement.mpn_norm) == (None, None)
    assert requirement.category_slug == "resistor"
    assert {item.template: item.value for item in requirement.filters} == {"resistance": "10k ±1%"}
    assert not requirement.rejections
    # A word that is not vocabulary either must not acquire one and become an MPN.
    assert parser.parse("gizmo 1%").mpn is None


def test_an_invented_part_number_cannot_pull_in_a_part_by_full_text(
    catalogue: Session, bin_: Location
) -> None:
    """The consequence: a 220 Ω part was the *sole* exact match for a 10 k line.

    `build_match_query('resistor ±1%')` became `'"resistor" "1"*'`, which the FTS
    index answered from the 220 Ω part's description ("1% thin film resistor"). The
    10 k part was not offered at all — its description says "thick film chip".
    """
    make_resistor(
        catalogue,
        "AAA-220R",
        resistance="220R",
        package="1206",
        description="1% thin film resistor",
        location=bin_,
        qty_milli=500_000,
    )
    make_resistor(
        catalogue,
        "BBB-10K",
        resistance="10k",
        package="0603",
        description="thick film chip",
        location=bin_,
        qty_milli=1_000,
    )
    catalogue.commit()

    answer = suggest(catalogue, "10k resistor 1%")

    assert "AAA-220R" not in _mpns(answer.in_stock)  # type: ignore[attr-defined]
    assert _mpns(answer.in_stock) == ["BBB-10K"]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 3-4. The wire shape the paste panel sends
# ---------------------------------------------------------------------------


def test_a_new_bom_line_without_a_quantity_is_refused_and_with_one_is_accepted(
    client: TestClient, db: Session
) -> None:
    """The defect: every accept in the paste panel 422'd, and its tests said 200.

    `BomLineEdit._create_needs_a_quantity_delete_needs_an_id` requires
    `qty_per_assembly_milli` whenever `id` is omitted, and it is a cross-field
    pydantic validator — invisible in the generated `schema.ts`, so `tsc` passed
    and the frontend tests pinned the rejected body against a stub that returned
    200. "Use this", "Accept all" and "Accept without a part" were all dead.

    This asserts the contract from the **server** side, which is the half a stubbed
    `fetch` can never cover, in both directions so the shape cannot drift again.
    """
    project = make_project(db)
    part = make_part(db, "PANEL-PART", mpn="PANEL-PART")
    db.commit()

    rejected = client.put(
        f"/api/projects/{project.id}/bom",
        json={"edits": [{"note": "100nF 0603 10% X7R", "part_id": part.id}], "client_op_id": "a"},
    )
    assert rejected.status_code == 422
    assert "qty_per_assembly_milli is required" in str(rejected.json())

    accepted = client.put(
        f"/api/projects/{project.id}/bom",
        json={
            "edits": [
                {
                    "note": "100nF 0603 10% X7R",
                    "part_id": part.id,
                    "qty_per_assembly_milli": 3000,
                }
            ],
            "client_op_id": "b",
        },
    )
    assert accepted.status_code == 200, accepted.json()
    (line,) = accepted.json()["lines"]
    assert (line["note"], line["part_id"], line["qty_per_assembly_milli"]) == (
        "100nF 0603 10% X7R",
        part.id,
        3000,
    )


def test_a_line_created_with_no_part_is_not_recorded_as_a_confirmed_match(
    client: TestClient, db: Session
) -> None:
    """What the bulk "add all" now sends, and why it is safe to send in bulk.

    `_apply_bom_line_edit` sets `is_match_confirmed = True` for any edit naming a
    part without saying otherwise — that is correct for a human clicking one
    candidate, and it is exactly why the bulk action must send **no part**. A rank
    is not a confirmation: `bom_import` refuses to set the flag even for an exact
    `mpn_norm` equality, and a ranking is a far weaker claim than that.
    """
    project = make_project(db)
    db.commit()

    response = client.put(
        f"/api/projects/{project.id}/bom",
        json={
            "edits": [
                {"note": "3x 10k 1% 0603 resistor", "part_id": None, "qty_per_assembly_milli": 3000}
            ],
            "client_op_id": "c",
        },
    )

    assert response.status_code == 200, response.json()
    (line,) = response.json()["lines"]
    assert line["part_id"] is None
    assert line["is_match_confirmed"] is False
    assert line["note"] == "3x 10k 1% 0603 resistor"


# ---------------------------------------------------------------------------
# 5. A sentence claiming containment where SQL only proved overlap
# ---------------------------------------------------------------------------


def test_the_range_overlap_explanation_claims_only_what_the_predicate_proved(
    catalogue: Session, bin_: Location
) -> None:
    """The defect: "Capacitance 20-100uF falls inside the 20-30uF asked for".

    False as written. `query_builder._substitution_predicate` for `range_overlap`
    is `value_min <= high AND value_max >= low` — an overlap test — so a 20-100 µF
    part qualifies against a 20-30 µF requirement by reaching into it, not by
    sitting inside it. `SubstitutionReason`'s whole claim is that the sentence
    "restates a predicate SQL enforced", and this is the sentence a user reads
    immediately before pressing "Use this".
    """
    assert (
        SubstitutionDirection(_template(catalogue, "capacitance").substitution_direction)
        is SubstitutionDirection.RANGE_OVERLAP
    ), "this test is about the range_overlap wording; capacitance must still use it"
    make_capacitor(
        catalogue,
        "WIDE-CAP",
        capacitance="20-100uF",
        voltage_rating="50V",
        location=bin_,
        qty_milli=10_000,
    )
    catalogue.commit()

    answer = suggest(catalogue, "20-30uF 25V ceramic capacitor")

    (candidate,) = answer.in_stock  # type: ignore[attr-defined]
    assert candidate.is_substitute
    reason = next(item for item in candidate.reasons if item.template == "capacitance")
    assert reason.direction is SubstitutionDirection.RANGE_OVERLAP
    assert reason.explanation == "Capacitance 20-100uF overlaps the 20-30uF asked for"
    assert "falls inside" not in reason.explanation
    assert "inside" not in reason.explanation


# ---------------------------------------------------------------------------
# 6. Bounds that did not bind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "assembly_count=200000",
        "assembly_count=1000000000000000000000000000000",
        "assembly_count=0",
        "candidates=51",
        "candidates=0",
    ],
)
def test_the_suggestions_route_enforces_its_own_query_bounds(
    client: TestClient, db: Session, query: str
) -> None:
    """The defect: every one of these returned 200 with the value used unclamped.

    `assembly_count: AssemblyCount = Query(default=1, ...)` — a bare type-alias
    annotation with a `Query()` *default* — does not propagate the alias's `Field`
    constraints in FastAPI/Pydantic v2, unlike the `offset: Annotated[ResultOffset,
    Query()]` two lines below it, which enforced its bound correctly all along.
    `assembly_count=10**30` multiplied straight into `required_milli` on a real
    response, which is precisely the class of input `app/api/limits.py` exists for.
    """
    project = make_project(db)
    make_bom_line(db, project, qty_per_assembly_milli=1000)
    db.commit()

    response = client.get(f"/api/projects/{project.id}/bom/suggestions?{query}")

    assert response.status_code == 422, response.json()


def test_the_suggestions_route_still_accepts_values_inside_its_bounds(
    client: TestClient, db: Session
) -> None:
    """The other direction, so the fix cannot be "reject everything"."""
    project = make_project(db)
    make_bom_line(db, project, qty_per_assembly_milli=1000)
    db.commit()

    response = client.get(
        f"/api/projects/{project.id}/bom/suggestions?assembly_count=100000&candidates=50"
    )

    assert response.status_code == 200, response.json()
    assert response.json()["assembly_count"] == 100_000


# ---------------------------------------------------------------------------
# 7. A preamble row beat the real header row
# ---------------------------------------------------------------------------

TITLE_BLOCK = (
    "Bill of Materials\n"
    "Part Number:,ASM-0012,Description:,Nightlight main board\n"
    "Date:,29-Jul-26,Revision:,B\n"
    "\n"
    "Designator,Comment,Footprint,Quantity,Manufacturer,Manufacturer Part Number\n"
    '"C1, C2",100nF,CAPC1608X90N,2,Samsung,CL10B104KB8NNNC\n'
    '"R1, R2, R3",10k,RESC1608X55N,3,Yageo,RC0603FR-0710KL\n'
    "U1,LM358,SOIC127P600X175-8N,1,onsemi,LM358DR2G\n"
)


def test_the_best_scoring_header_row_wins_over_an_earlier_title_block() -> None:
    """The defect: a 2-column Excel title block outranked the 6-column header.

    `_probe` returned the **first** row in the search window that mapped
    `_MIN_HEADER_FIELDS`, never the best-scoring one — even though `_best_probe`
    already scores candidates on `len(mapping)` across *delimiters*. So `Part
    Number:` won the MPN slot, `Description:` won description, the real header was
    consumed as a data line, every designator cell landed in `mpn_raw` and was fed
    to `_match_lines`' exact-`mpn_norm` lookup, every real MPN went to `column_6`
    in the raw fields, and every quantity became the `FALLBACK` 1 000 while the
    file said 2, 3, 1.

    **And nothing named the header choice.** The two warnings that did fire read
    like ordinary malformed-export noise. This is the "a wrongly mapped column
    looks right" failure the module docstring says has no recovery.
    """
    parsed = parse_bom(TITLE_BLOCK)

    assert {field.value: header for field, header in parsed.columns.items()} == {
        "designators": "Designator",
        "value": "Comment",
        "footprint": "Footprint",
        "quantity": "Quantity",
        "manufacturer": "Manufacturer",
        "mpn": "Manufacturer Part Number",
    }
    assert [line.mpn_raw for line in parsed.lines] == [
        "CL10B104KB8NNNC",
        "RC0603FR-0710KL",
        "LM358DR2G",
    ]
    assert [line.designators for line in parsed.lines] == ["C1, C2", "R1, R2, R3", "U1"]
    assert [line.qty_per_assembly_milli for line in parsed.lines] == [2000, 3000, 1000]
    # The title block is preamble, not data, and the rival is named out loud so a
    # user can tell a title block from a genuine header problem.
    assert len(parsed.preamble) == 4
    assert any("also looked like one" in warning for warning in parsed.warnings)


def test_an_earlier_row_still_wins_a_tie_on_recognised_columns() -> None:
    """The tie-break, so "best scoring" did not become "prefer a later row".

    A real header row sits above its data, and two rows mapping the same number of
    columns is the evidence failing to distinguish them — not a reason to pick the
    second.
    """
    parsed = parse_bom("Reference,Value\nR1,10k\nReference,Value\nR2,4k7\n")

    assert parsed.preamble == ()
    assert [line.designators for line in parsed.lines] == ["R1", "Reference", "R2"]


# ---------------------------------------------------------------------------
# 8. A UTF-16 export imported "cleanly", with NULs in every column
# ---------------------------------------------------------------------------

UTF16_SOURCE = (
    "Designator\tComment\tFootprint\tQuantity\r\nC1\t100nF\tC0603\t1\r\nR1\t10k\tR0603\t2\r\n"
).encode("utf-16")


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(UTF16_SOURCE, id="raw-bytes"),
        pytest.param(UTF16_SOURCE.decode("cp1252"), id="cp1252-decoded"),
        # What the shipped frontend actually produces: `FileReader.readAsText`
        # with no encoding argument.
        pytest.param(UTF16_SOURCE.decode("utf-8", errors="replace"), id="utf8-replaced"),
    ],
)
def test_a_utf16_export_is_refused_by_name_rather_than_imported_mangled(
    shape: str | bytes,
) -> None:
    """The defect: status 200, six lines, zero warnings, NULs in every column.

    Three guarantees failed at once and it was the worst of the four format cases,
    because the XLSX one it *does* catch at least says nothing was imported.
    `_BINARY_SIGNATURES` did not know the UTF-16 BOMs; `_decode` then succeeded via
    `cp1252` (NUL is a valid cp1252 byte) so no "undecodable bytes" warning fired
    either; and `_normalize_header` strips everything outside `[0-9a-z]`, which
    **deletes the interleaved NULs** — so the mangled headers mapped perfectly and
    the import looked clean while writing `'\\x00C\\x001\\x00'` into `designators`,
    `ref_value`, `footprint` and the `raw_fields_json` keys.
    """
    parsed = parse_bom(shape)

    assert parsed.lines == ()
    assert parsed.columns == {}
    (warning,) = parsed.warnings
    assert "UTF-16" in warning
    assert "nothing was imported" in warning


def test_an_ordinary_tab_delimited_export_is_not_mistaken_for_one() -> None:
    """The other direction: the sniffer must not refuse the format it recommends."""
    parsed = parse_bom("Designator\tComment\tFootprint\tQuantity\nC1\t100nF\tC0603\t1\n")

    assert len(parsed.lines) == 1
    assert parsed.warnings == ()


# ---------------------------------------------------------------------------
# 9. One unbalanced quote ate 3 993 components, silently
# ---------------------------------------------------------------------------


def test_a_quote_that_absorbs_the_rest_of_the_file_says_so() -> None:
    """The defect: 4 000 data rows became 7 lines, and the only warning was a
    *footprint truncation* — which reads like a cosmetic column-width note.

    The comment at `_MAX_FIELD_CHARS` names this exact scenario ("a 4000-line
    export with a single stray `\\"` was enough"): raising the field cap stopped the
    `_csv.Error`, but the silent truncation of the BOM was never addressed. The
    evidence was already computed and thrown away — `_probe(",")` returned 8 rows
    while `_probe(";")`, `_probe("\\t")` and `_probe("|")` each returned 4 001 on
    the same text.
    """
    rows = ["Reference,Value,Footprint,Qty,MPN"]
    for index in range(1, 4001):
        if index == 7:
            rows.append('R7,10k,"Resistor 1% wide,1,')
        else:
            rows.append(f"R{index},10k,R_0603,1,RC0603FR-0710KL")
    parsed = parse_bom("\n".join(rows) + "\n")

    matching_warnings = [
        warning for warning in parsed.warnings if "were absorbed into one cell" in warning
    ]
    assert matching_warnings, f"the loss was not reported at all: {parsed.warnings}"
    (absorbed,) = matching_warnings
    assert "4001 lines" in absorbed
    assert "8 rows" in absorbed
    assert "row 8" in absorbed
    assert "3993" in absorbed


def test_a_legitimate_two_line_quoted_cell_is_not_reported_as_damage() -> None:
    """A quoted cell spanning one newline is legal and common, so it stays quiet.

    Without this the fix would trade a silent loss for a warning on every export
    carrying a wrapped description, which trains the user to ignore the one that
    matters.
    """
    parsed = parse_bom(
        'Reference,Value,Description\nR1,10k,"thin film,\nwide body"\nR2,4k7,plain\n'
    )

    assert len(parsed.lines) == 2
    assert not any("absorbed" in warning for warning in parsed.warnings)


# ---------------------------------------------------------------------------
# 10. A designator cell that was not a designator list, over-stating demand
# ---------------------------------------------------------------------------

FRENCH_TEMPLATE = (
    "Item,Repere,Designation,Package,Qty\n"
    "1,C1,Condensateur ceramique 100nF 50V X7R,0603,1\n"
    "2,R1 R2 R3,Resistance couche epaisse 10k 1%,0603,3\n"
    "3,U1,Amplificateur operationnel LM358,SOIC-8,1\n"
)


def test_a_description_column_named_designation_does_not_become_the_demand() -> None:
    """The defect: five French words counted as five designators, out-voting `Qty`.

    `designation` was a `DESIGNATORS` alias, and in French and other European BOM
    templates *Designation* is the **description** column. With no rival designator
    alias present it won outright, no warning: the real designator column (`Repere`)
    went unmapped, `_expand_designators` accepted arbitrary prose, and
    `_resolve_qty`'s take-the-larger rule then preferred its word count over an
    explicit quantity — so a line the file says is 1-off demanded 5, with a warning
    asserting "5 designators listed" about five French words.

    Both halves are fixed and both are asserted: the alias moved to `DESCRIPTION`
    (the slot it is safe to be wrong in, since nothing is derived from it), and
    `_expand_designators` now requires designator-shaped tokens, which is what
    makes `_resolve_qty`'s asymmetry sound in the first place.
    """
    parsed = parse_bom(FRENCH_TEMPLATE)

    # The prose column describes; it does not designate. Asserted as the whole
    # mapping so a regression names which slot it went back to.
    assert {field.value: header for field, header in parsed.columns.items()} == {
        "description": "Designation",
        "footprint": "Package",
        "quantity": "Qty",
    }
    assert [line.qty_per_assembly_milli for line in parsed.lines] == [1000, 3000, 1000]
    assert [line.description for line in parsed.lines] == [
        "Condensateur ceramique 100nF 50V X7R",
        "Resistance couche epaisse 10k 1%",
        "Amplificateur operationnel LM358",
    ]
    assert not any("designators listed" in warning for warning in parsed.all_warnings)


def test_prose_in_a_designator_column_is_not_counted_and_says_why() -> None:
    """The half that generalises past the alias.

    Any wrongly mapped column can land here — an item number, a note, a
    description under a different name — and the count it produces must not
    override a declared quantity *upward*, because `_resolve_qty`'s asymmetry
    assumes both numbers are real statements about the board.
    """
    parsed = parse_bom(
        "Reference,Value,Qty\nCondensateur ceramique 100nF,100nF,1\nR1 R2 R3,10k,3\n"
    )

    prose, real = parsed.lines
    assert prose.qty_per_assembly_milli == 1000, "the declared 1 wins, not a word count"
    assert any("nothing is shaped like a reference designator" in w for w in prose.warnings)
    # A genuine designator list is untouched, so the fix did not disable the check.
    assert real.qty_per_assembly_milli == 3000


def test_a_designator_range_and_a_padded_reference_still_expand() -> None:
    """The shape gate must not break what `_expand_designators` exists to do."""
    parsed = parse_bom("Reference,Value,Qty\nC1-C4,100nF,4\nR01-R03,10k,3\nTP1 TP2,,2\n")

    assert [line.qty_per_assembly_milli for line in parsed.lines] == [4000, 3000, 2000]
    assert not any("shaped like" in warning for warning in parsed.all_warnings)


# ---------------------------------------------------------------------------
# The claim underneath all of this
# ---------------------------------------------------------------------------


def test_no_model_output_can_reach_a_bom_line_or_a_parameter_value(db: Session) -> None:
    """The primary question the review was asked, as an assertion.

    There is no model path into a BOM line or into `parameter_value` on this
    branch: `app.services.requirements.interpret` is imported by nothing but its
    own unit test, no route calls `interpret` or `apply_interpretation`, and
    `matching` never writes. What the review found instead was the *deterministic*
    parser inventing predicates and part numbers, and a ranking being recorded as a
    human's choice — which is why the fixes above are structural and none of them
    is a threshold.

    Kept as a test rather than a comment because the seam is designed to be filled
    in later (ADR 0005: a Job that releases the GPU, never the API process), and
    the day it is, this is the line that has to still hold.
    """
    from app.api.routes import projects, requirements
    from app.services.requirements import matching

    for module in (projects, requirements, matching):
        source = module.__dict__
        assert "interpret" not in source, f"{module.__name__} reached the model seam"
        assert "apply_interpretation" not in source, f"{module.__name__} applies a model's answer"

    # And the one field a model could never produce even if it were wired in:
    # `Requirement.mpn` is set by the grammar alone, so a filled-in interpreter
    # cannot name a part. `with_filters` is the whole set of things it may change.
    from app.services.requirements import parser as parser_module

    vocabulary = load_vocabulary(db)
    requirement = DeterministicRequirementParser(vocabulary).parse("2x LM358N")
    rebuilt = parser_module.with_filters(
        requirement, filters=(), category=None, residue=(), rejections=(), notes=()
    )
    assert rebuilt.mpn_norm == requirement.mpn_norm == "lm358n"
    assert rebuilt.quantity == requirement.quantity == 2
    assert rebuilt.text == requirement.text == "2x LM358N"


def test_the_parametric_filter_is_still_the_only_thing_that_decides(
    catalogue: Session, bin_: Location
) -> None:
    """A part engineered to win every ranking term, excluded by one predicate.

    `test_suggestions.py` already pins this; it is repeated here because three of
    the ten findings were the *requirement* being emptied or widened before it ever
    reached the executor. A filter that decides correctly over a requirement that
    lost its predicates is no protection at all, so the two claims belong next to
    each other.
    """
    make_capacitor(
        catalogue,
        "PLAUSIBLE-1206",
        package="1206",
        dielectric="X7R",
        location=bin_,
        qty_milli=10_000_000,
    )
    make_capacitor(
        catalogue,
        "CORRECT-0603",
        package="0603",
        dielectric="X7R",
        location=bin_,
        qty_milli=1_000,
    )
    catalogue.commit()

    answer = suggest(catalogue, "100nF 0603 10% X7R 25V ceramic capacitor")

    assert _mpns(answer.in_stock) == ["CORRECT-0603"]  # type: ignore[attr-defined]
    assert _mpns(answer.not_stocked) == []  # type: ignore[attr-defined]


def test_a_numeric_parameter_value_written_by_these_tests_has_both_bounds(
    catalogue: Session, bin_: Location
) -> None:
    """The invariant every fixture in this file depends on, asserted once.

    A null-bounded numeric `parameter_value` is invisible to every range query,
    silently — so a regression test that wrote one would pass by finding nothing.
    """
    from sqlalchemy import select

    from app.models.parameter import ParameterTemplate, ParameterValue

    make_resistor(catalogue, "BOUNDS-CHECK", resistance="10k", package="0603")
    make_capacitor(catalogue, "BOUNDS-CAP", capacitance="20-30uF")
    catalogue.commit()

    numeric = catalogue.execute(
        select(ParameterValue)
        .join(ParameterTemplate, ParameterTemplate.id == ParameterValue.template_id)
        .where(ParameterTemplate.value_type == ValueType.NUMERIC.value)
    ).scalars()
    rows = list(numeric)
    assert rows, "no numeric parameter_value rows were written; the fixtures are inert"
    for row in rows:
        assert row.value_min is not None, row.raw_input
        assert row.value_max is not None, row.raw_input
