"""Turning a requirement into candidates — and the properties that must not bend.

The load-bearing test is `test_ranking_never_offers_a_part_the_filter_excluded`.
Everything else in this file is about being *useful*; that one is about being
*correct*, and it is the one `CLAUDE.md`'s deterministic rule reduces to at this
layer: a plausible substitute with the wrong rating is a field failure, so no
amount of ranking may promote a part `app.services.search.query_builder` left out.

The fixture seeds **only** the templates and categories, not `seed_demo`'s sample
parts. Every part here is built by the test that needs it, because most of these
assertions are about the *order* of two rows that differ in exactly one way, and a
background catalogue of near-identical capacitors makes that impossible to state.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Part, PartCategory
from app.models.enums import ValueType
from app.models.parameter import ParameterTemplate
from app.models.storage import Location
from app.scripts.seed_demo import seed_categories, seed_parameter_templates
from app.services import parameters
from app.services.requirements import matching
from app.services.requirements.matching import Outcome, RequirementInput, suggest_batch
from app.services.search.query_builder import SearchQuery, execute
from tests.factories import make_bom_line, make_location, make_lot, make_part, make_project

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def catalogue(db: Session) -> Session:
    """Templates and categories only — the vocabulary, with an empty parts table."""
    seed_categories(db)
    seed_parameter_templates(db)
    db.commit()
    return db


@pytest.fixture
def bin_(catalogue: Session) -> Location:
    return make_location(catalogue, "Suggestion bin")


def _template(db: Session, name: str) -> ParameterTemplate:
    return db.execute(select(ParameterTemplate).where(ParameterTemplate.name == name)).scalar_one()


def _category_id(db: Session, slug: str) -> int:
    return int(db.execute(select(PartCategory.id).where(PartCategory.slug == slug)).scalar_one())


def make_capacitor(
    db: Session,
    mpn: str,
    *,
    capacitance: str = "100nF",
    voltage_rating: str | None = "25V",
    capacitor_technology: str | None = "ceramic",
    package: str | None = None,
    dielectric: str | None = None,
    location: Location | None = None,
    qty_milli: int = 0,
    reserved_milli: int = 0,
    is_stub: bool = False,
    name: str | None = None,
) -> Part:
    """A capacitor with exactly the facets named, and optionally stock.

    Values go through `app.services.parameters`, never a hand-built
    `parameter_value`: every numeric row needs `value_min`/`value_max` populated or
    it is silently invisible to every range query, and a fixture that wrote them
    itself would be testing a shape no write path produces.
    """
    part = make_part(
        db,
        name or mpn,
        mpn=mpn,
        category_id=_category_id(db, "capacitor"),
        is_stub=is_stub,
    )
    for template_name, raw in (
        ("capacitance", capacitance),
        ("voltage_rating", voltage_rating),
        ("capacitor_technology", capacitor_technology),
        ("package", package),
        ("dielectric", dielectric),
    ):
        if raw is None:
            continue
        template = _template(db, template_name)
        if template.value_type == ValueType.NUMERIC:
            parameters.set_numeric(db, part, template, raw)
        else:
            parameters.set_choice(db, part, template, raw)
    if location is not None and qty_milli:
        lot = make_lot(db, part, location, qty_milli=qty_milli)
        lot.qty_reserved_milli_cached = reserved_milli
    db.flush()
    return part


def suggest(db: Session, text: str, **kwargs: object) -> matching.Suggestion:
    """One line, through the batch door — there is only one code path."""
    limit = kwargs.pop("limit", matching.DEFAULT_LIMIT)
    assert isinstance(limit, int)
    required_milli = kwargs.pop("required_milli", None)
    assert required_milli is None or isinstance(required_milli, int)
    assert not kwargs, kwargs
    return suggest_batch(
        db, [RequirementInput(text=text, required_milli=required_milli)], limit=limit
    )[0]


def mpns(candidates: tuple[matching.Candidate, ...]) -> list[str | None]:
    return [candidate.part.mpn for candidate in candidates]


# ---------------------------------------------------------------------------
# The one that matters: ranking cannot override the filter
# ---------------------------------------------------------------------------


def test_ranking_never_offers_a_part_the_filter_excluded(
    catalogue: Session, bin_: Location
) -> None:
    """A part built to win **every** ranking term, excluded by one predicate.

    `WINNER` is in stock with ten thousand units, unreserved, not a stub, and its
    capacitance, voltage, dielectric and technology are all exactly what the line
    asks for. It differs on `package` alone. Every term of the sort key prefers it
    to `HUMBLE`, which has one unit of stock — so if membership and ordering were
    ever fused, or if the ranking assembled its own list, this is the row that
    would come back first.

    Asserted in both directions: excluded from `in_stock` *and* from
    `not_stocked` (a suggestion nobody has is still a suggestion), and `HUMBLE`
    present, so the test cannot pass by the whole feature returning nothing.
    """
    make_capacitor(
        catalogue,
        "WINNER",
        package="1206",
        dielectric="X7R",
        location=bin_,
        qty_milli=10_000_000,
    )
    make_capacitor(
        catalogue, "HUMBLE", package="0603", dielectric="X7R", location=bin_, qty_milli=1_000
    )
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V X7R 0603 ceramic capacitor")

    assert mpns(answer.in_stock) == ["HUMBLE"]
    assert mpns(answer.not_stocked) == []
    assert "WINNER" not in mpns(answer.in_stock) + mpns(answer.not_stocked)


def test_a_substitute_ranks_below_an_exact_match_however_much_of_it_you_have(
    catalogue: Session, bin_: Location
) -> None:
    """Correctness of fit sits **above** convenience — terms 1 and 2 over term 3.

    The 50 V part qualifies (`voltage_rating` is `higher_ok`) and there is ten
    thousand times more of it. It still ranks second, because a part matching the
    line as written needs no decision from the user and a substitute does, and no
    amount of stock is worth making that decision for them.

    Note this pins the *pair* of correctness terms against the quantity term
    rather than either one alone: exactness and a zero distance coincide today
    (see the module docstring on term 1 being a deliberate redundancy, and
    `test_an_exact_match_is_a_candidate_at_zero_distance` for the property that
    makes it one), so removing one leaves the other doing the work. Moving stock
    above them is what this refuses.
    """
    make_capacitor(catalogue, "EXACT-25V", location=bin_, qty_milli=1_000)
    make_capacitor(
        catalogue, "SUBST-50V", voltage_rating="50V", location=bin_, qty_milli=10_000_000
    )
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V ceramic capacitor")

    assert mpns(answer.in_stock) == ["EXACT-25V", "SUBST-50V"]
    assert [candidate.is_substitute for candidate in answer.in_stock] == [False, True]
    assert [candidate.rank for candidate in answer.in_stock] == [1, 2]


def test_an_exact_match_is_a_candidate_at_zero_distance(catalogue: Session, bin_: Location) -> None:
    """The equivalence that makes ranking term 1 a redundancy rather than a rule.

    A part is reached only in `substitute` mode exactly when a numeric filter
    failed the search-mode overlap test — which is the same condition `_distance`
    scores above zero for. The two are computed by different code (SQL in
    `query_builder._numeric_predicate`, Python in `matching._distance`), and this
    is what says they still agree: if they drift, the ranking quietly starts
    depending on term 1 alone, and this goes red first.

    The requirement is a **band** and the exact part sits off its centre, which is
    what makes both directions of the equivalence load-bearing. A scalar `25V`
    against a 25 V part would score zero from the log-ratio alone, so it would pass
    even with the overlap rule removed; 22 µF against `20-30uF` scores zero only
    because the intervals overlap.
    """
    make_capacitor(catalogue, "IN-BAND-25V", capacitance="22uF", location=bin_, qty_milli=1_000)
    make_capacitor(
        catalogue,
        "SUBST-50V",
        capacitance="22uF",
        voltage_rating="50V",
        location=bin_,
        qty_milli=1_000,
    )
    make_capacitor(
        catalogue,
        "SUBST-1KV",
        capacitance="22uF",
        voltage_rating="1kV",
        location=bin_,
        qty_milli=1_000,
    )
    catalogue.commit()

    answer = suggest(catalogue, "20-30uF 25V ceramic capacitor")

    assert len(answer.in_stock) == 3
    for candidate in answer.in_stock:
        assert candidate.is_substitute == (candidate.distance > 0.0), candidate.part.mpn


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_an_in_stock_part_outranks_an_out_of_stock_one(catalogue: Session, bin_: Location) -> None:
    """Ranking term 1, and the split that materialises it.

    Two parts identical in every respect the filter and the other five terms can
    see. The only difference is a lot, and it decides which list each lands in —
    so "in stock first" is not a preference the ordering can lose, it is the
    partition.
    """
    make_capacitor(catalogue, "ON-SHELF", location=bin_, qty_milli=5_000)
    make_capacitor(catalogue, "CATALOGUE-ONLY")
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V ceramic capacitor")

    assert answer.outcome is Outcome.STOCKED
    assert mpns(answer.in_stock) == ["ON-SHELF"]
    assert mpns(answer.not_stocked) == ["CATALOGUE-ONLY"]
    assert answer.in_stock[0].qty_milli == 5_000
    assert answer.not_stocked[0].qty_milli == 0


def test_owning_nothing_that_satisfies_a_line_is_a_useful_answer(catalogue: Session) -> None:
    """The answer that turns into an order, and the reason it is not an empty list.

    Two parts satisfy the line and there is no stock of either. An empty
    `in_stock` alone would be indistinguishable from "this part does not exist",
    which is a different problem with a different fix — so the outcome is its own
    word and the parts that *would* work come back labelled.
    """
    make_capacitor(catalogue, "ORDER-ME-25V")
    make_capacitor(catalogue, "ORDER-ME-50V", voltage_rating="50V")
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V ceramic capacitor")

    assert answer.outcome is Outcome.ORDER
    assert answer.in_stock == ()
    assert sorted(mpns(answer.not_stocked)) == ["ORDER-ME-25V", "ORDER-ME-50V"]
    assert "you own nothing that satisfies this" in answer.message


def test_no_match_at_all_is_distinguished_from_being_out_of_stock(
    catalogue: Session, bin_: Location
) -> None:
    """`no_match` versus `order`: a missing part is not a stock problem.

    The catalogue holds a well-stocked 100 nF part and the line asks for 470 µF.
    Nothing satisfies it in either mode, so buying the parts already known about
    would not fix the line — somebody has to add one, and the message says so
    rather than reading as a shortage.
    """
    make_capacitor(catalogue, "WRONG-VALUE", location=bin_, qty_milli=500_000)
    catalogue.commit()

    answer = suggest(catalogue, "470uF 25V ceramic capacitor")

    assert answer.outcome is Outcome.NO_MATCH
    assert (answer.in_stock, answer.not_stocked) == ((), ())
    assert "has to be added" in answer.message


def test_the_availability_split_agrees_with_in_stock_only(
    catalogue: Session, bin_: Location
) -> None:
    """The drift guard for running one executor pass instead of two.

    `matching` fetches with `in_stock_only=False` and partitions locally on
    `qty_milli_cached > 0`. That is the same predicate the flag compiles to, and
    this is what keeps it the same: the `in_stock` list must be exactly what the
    executor returns when *it* is asked to filter on stock, across both modes —
    `STOCKED-B` is only reachable in `substitute` mode, so a comparison against
    `search` alone would pass while the split was wrong.
    """
    make_capacitor(catalogue, "STOCKED-A", location=bin_, qty_milli=3_000)
    make_capacitor(catalogue, "STOCKED-B", voltage_rating="50V", location=bin_, qty_milli=7_000)
    make_capacitor(catalogue, "BARE-C")
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V ceramic capacitor")
    by_executor = {
        part.mpn
        for mode in ("search", "substitute")
        for part in execute(
            catalogue,
            SearchQuery(
                filters=answer.requirement.to_filters(),
                category_slug=answer.requirement.category_slug,
                in_stock_only=True,
                mode=mode,
            ),
        )
    }

    assert set(mpns(answer.in_stock)) == by_executor
    assert mpns(answer.not_stocked) == ["BARE-C"]


def test_stock_already_promised_to_a_build_does_not_win_the_quantity_term(
    catalogue: Session, bin_: Location
) -> None:
    """Ranking term 4 reads *free* stock, not the cached balance.

    Both parts hold five thousand; one of them is entirely reserved. Ranking the
    reserved lot first would send a picker to a drawer whose contents are already
    spoken for, which is the whole reason `qty_reserved_milli_cached` exists.
    """
    make_capacitor(catalogue, "ALL-RESERVED", location=bin_, qty_milli=5_000, reserved_milli=5_000)
    make_capacitor(catalogue, "FREE", location=bin_, qty_milli=5_000)
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V ceramic capacitor", required_milli=2_000)

    assert mpns(answer.in_stock) == ["FREE", "ALL-RESERVED"]
    assert [candidate.covers_required for candidate in answer.in_stock] == [True, False]


# ---------------------------------------------------------------------------
# Substitutes, and saying why
# ---------------------------------------------------------------------------


def test_a_substitute_is_offered_with_a_stated_reason(catalogue: Session, bin_: Location) -> None:
    """The explanation is what makes a suggestion trustworthy rather than magical.

    A 50 V part for a 25 V line qualifies because `voltage_rating` is declared
    `higher_ok` — so the reason names the direction, quotes both values, and reads
    as a restatement of the predicate the executor applied. Every other filter is
    explained too: "exactly the ceramic asked for" is half the justification, and
    a UI showing only the voltage line would leave the user to wonder what else
    was traded away.
    """
    make_capacitor(catalogue, "SUBST-50V", voltage_rating="50V", location=bin_, qty_milli=1_000)
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V ceramic capacitor")

    assert mpns(answer.in_stock) == ["SUBST-50V"]
    candidate = answer.in_stock[0]
    assert candidate.is_substitute

    reasons = {reason.template: reason for reason in candidate.reasons}
    assert set(reasons) == {"capacitance", "voltage_rating", "capacitor_technology"}

    voltage = reasons["voltage_rating"]
    assert voltage.direction.value == "higher_ok"
    assert (voltage.required, voltage.offered) == ("25V", "50V")
    assert "at or above" in voltage.explanation
    assert "higher rating satisfies a lower requirement" in voltage.explanation

    assert "exactly" in reasons["capacitor_technology"].explanation
    assert reasons["capacitor_technology"].offered == "ceramic"


def test_an_exact_match_carries_no_explanation(catalogue: Session, bin_: Location) -> None:
    """Nothing to justify: it is what was asked for.

    Stated as a test because the alternative — a sentence on every row — trains a
    reader to skip the ones that matter, and `reasons` being non-empty is exactly
    how a UI decides a row needs reading.
    """
    make_capacitor(catalogue, "EXACT-25V", location=bin_, qty_milli=1_000)
    catalogue.commit()

    candidate = suggest(catalogue, "100nF 25V ceramic capacitor").in_stock[0]

    assert not candidate.is_substitute
    assert candidate.reasons == ()


def test_the_least_over_specified_substitute_ranks_first(
    catalogue: Session, bin_: Location
) -> None:
    """Ranking term 3: closeness, in decades.

    Both parts satisfy a 25 V line and both hold the same stock, so the only term
    left is distance. The 50 V part wins because a 1 kV capacitor is bigger,
    dearer and no more correct — which is what the log-ratio distance encodes, and
    what a linear one would understate.
    """
    make_capacitor(catalogue, "SUBST-1KV", voltage_rating="1kV", location=bin_, qty_milli=4_000)
    make_capacitor(catalogue, "SUBST-50V", voltage_rating="50V", location=bin_, qty_milli=4_000)
    catalogue.commit()

    answer = suggest(catalogue, "100nF 25V ceramic capacitor")

    assert mpns(answer.in_stock) == ["SUBST-50V", "SUBST-1KV"]
    assert answer.in_stock[0].distance < answer.in_stock[1].distance


def test_a_part_inside_the_requested_band_has_no_distance(
    catalogue: Session, bin_: Location
) -> None:
    """Zero for an overlap, because a part in the band *is* what was asked for.

    `20-30uF` and a 22 µF part: there is nothing to prefer on value, so distance
    must not manufacture an ordering out of where in the band the part happens to
    sit — the tie-breaks below it are the honest ones.
    """
    make_capacitor(catalogue, "IN-BAND", capacitance="22uF", location=bin_, qty_milli=1_000)
    catalogue.commit()

    answer = suggest(catalogue, "20-30uF 25V ceramic capacitor")

    assert mpns(answer.in_stock) == ["IN-BAND"]
    assert answer.in_stock[0].distance == 0.0


# ---------------------------------------------------------------------------
# Lines nothing can be done with
# ---------------------------------------------------------------------------


def test_an_unreadable_line_is_never_answered_with_the_whole_catalogue(
    catalogue: Session, bin_: Location
) -> None:
    """The guard on an empty `SearchQuery`.

    A requirement with no filters, no category and no part number would build a
    query with no predicates, which the executor answers with everything. Five
    arbitrary parts is the worst possible answer to "I did not understand this" —
    so the line reaches no query at all and says so, keeping its text.
    """
    make_capacitor(catalogue, "IRRELEVANT", location=bin_, qty_milli=9_000)
    catalogue.commit()

    answer = suggest(catalogue, "that thing Dave used on the mixer board")

    assert answer.outcome is Outcome.NOT_ACTIONABLE
    assert (answer.in_stock, answer.not_stocked) == ((), ())
    assert answer.requirement.text == "that thing Dave used on the mixer board"
    assert answer.requirement.residue
    assert "nothing in this description could be turned into a search" in answer.message


def test_a_part_number_is_a_filter_not_a_bypass(catalogue: Session, bin_: Location) -> None:
    """`Requirement.mpn` reaches the executor as `SearchQuery.text`, ANDed.

    Two halves. A bare part number is searchable — it makes the line actionable
    without an empty query — and a part number the catalogue does not have comes
    back `no_match` rather than as the whole shelf.

    The second half is the important one: an MPN lookup running *beside* the
    parametric filter is how a suggestion starts being offered that the filter
    excluded, which is the failure this whole layer exists to prevent.
    """
    make_capacitor(catalogue, "LM358N-CAPS", location=bin_, qty_milli=2_000)
    catalogue.commit()

    found = suggest(catalogue, "LM358N-CAPS")
    assert found.requirement.mpn == "LM358N-CAPS"
    assert mpns(found.in_stock) == ["LM358N-CAPS"]

    missing = suggest(catalogue, "ZZQ9871XT")
    assert missing.requirement.mpn == "ZZQ9871XT"
    assert missing.outcome is Outcome.NO_MATCH
    assert (missing.in_stock, missing.not_stocked) == ((), ())


def test_a_refused_value_narrows_nothing_rather_than_narrowing_wrongly(
    catalogue: Session, bin_: Location
) -> None:
    """A rejection really does drop the predicate, and travels with the answer.

    `1M` under farads is a megafarad. The line still searches on what it *could*
    read, so the result is broad — and the refusal is on the requirement so the
    caller can say why, instead of the breadth looking like a bug.
    """
    make_capacitor(catalogue, "SOME-CERAMIC", location=bin_, qty_milli=1_000)
    catalogue.commit()

    answer = suggest(catalogue, "1M ceramic capacitor")

    assert [item.reason for item in answer.requirement.rejections] == ["implausible"]
    assert mpns(answer.in_stock) == ["SOME-CERAMIC"]


# ---------------------------------------------------------------------------
# The batch
# ---------------------------------------------------------------------------


def test_twenty_lines_resolve_in_one_call(
    catalogue: Session, bin_: Location, client: TestClient
) -> None:
    """The shape an agent needs: a whole BOM answered by one request.

    Twenty distinct lines, one POST, twenty answers in request order with `index`
    on each so a client that reorders can still map them back. Half of them are
    stocked and half are not, so the response has to carry both outcomes at once
    rather than one status for the batch.
    """
    for index in range(10):
        make_capacitor(
            catalogue,
            f"STOCKED-{index}",
            capacitance=f"{index + 1}nF",
            location=bin_,
            qty_milli=1_000,
        )
        make_capacitor(catalogue, f"BARE-{index}", capacitance=f"{index + 1}uF")
    catalogue.commit()

    lines = [{"text": f"{index + 1}nF 25V ceramic capacitor"} for index in range(10)]
    lines += [{"text": f"{index + 1}uF 25V ceramic capacitor"} for index in range(10)]

    response = client.post("/api/requirements/suggest", json={"lines": lines})
    assert response.status_code == 200, response.text

    answers = response.json()["lines"]
    assert len(answers) == 20
    assert [answer["index"] for answer in answers] == list(range(20))
    assert [answer["outcome"] for answer in answers] == ["stocked"] * 10 + ["order"] * 10
    assert [answer["text"] for answer in answers] == [line["text"] for line in lines]


def test_a_batch_reads_the_vocabulary_once(
    catalogue: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What makes a batch cheaper than a loop, asserted rather than assumed.

    `load_vocabulary` is three queries and builds a phrase index; doing it per
    line would make a twenty-line request twenty times more expensive than it
    needs to be while looking identical from outside.
    """
    calls = 0
    real = matching.load_vocabulary

    def counted(session: Session) -> object:
        nonlocal calls
        calls += 1
        return real(session)

    monkeypatch.setattr(matching, "load_vocabulary", counted)

    suggest_batch(
        catalogue,
        [RequirementInput(text=f"{index + 1}nF 25V ceramic capacitor") for index in range(20)],
    )

    assert calls == 1


def test_a_batch_beyond_the_cap_is_refused_as_a_bad_request(client: TestClient) -> None:
    """422, not a slow success. Each line costs two executor queries."""
    lines = [{"text": "100nF"} for _ in range(matching.MAX_BATCH + 1)]
    assert client.post("/api/requirements/suggest", json={"lines": lines}).status_code == 422


# ---------------------------------------------------------------------------
# Parsing on its own
# ---------------------------------------------------------------------------


def test_parse_returns_the_requirement_without_searching(
    catalogue: Session, client: TestClient
) -> None:
    """The translation, visible before anything is matched against it."""
    response = client.post(
        "/api/requirements/parse",
        json={"lines": ["3x 100nF 50V X7R 0603", "that thing Dave used"]},
    )
    assert response.status_code == 200, response.text

    first, second = response.json()["requirements"]
    assert first["quantity"] == 3
    assert first["category"] == "capacitor"
    assert first["is_complete"] is True
    assert first["confidence"] == 1.0
    assert {item["template"] for item in first["filters"]} == {
        "capacitance",
        "voltage_rating",
        "dielectric",
        "package",
    }

    assert second["is_actionable"] is False
    assert second["residue"]
    assert second["text"] == "that thing Dave used"


# ---------------------------------------------------------------------------
# The BOM door, and accepting through the existing path
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_a_line(catalogue: Session, bin_: Location) -> Iterator[tuple[int, int, int]]:
    """A project, one unmatched BOM line, and a part that satisfies it.

    Yields `(project_id, bom_line_id, part_id)`.
    """
    part = make_capacitor(
        catalogue, "BOM-CANDIDATE", package="0603", location=bin_, qty_milli=500_000
    )
    project = make_project(catalogue, "Suggestion board")
    line = make_bom_line(
        catalogue,
        project,
        qty_per_assembly_milli=3_000,
        line_no=1,
        designators="C1,C2,C3",
        ref_value="100nF",
        footprint="C_0603_1608Metric",
        description="CAP CER 25V ceramic",
    )
    catalogue.commit()
    yield project.id, line.id, part.id


def test_bom_suggestions_answer_the_unmatched_lines(
    project_with_a_line: tuple[int, int, int], client: TestClient
) -> None:
    project_id, line_id, part_id = project_with_a_line

    response = client.get(f"/api/projects/{project_id}/bom/suggestions")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["total"] == 1
    (answer,) = body["lines"]
    assert answer["bom_line_id"] == line_id
    assert answer["outcome"] == "stocked"
    assert [candidate["part_id"] for candidate in answer["in_stock"]] == [part_id]


def test_a_footprint_is_never_read_as_a_part_number(
    project_with_a_line: tuple[int, int, int], client: TestClient
) -> None:
    """`C_0603_1608Metric` is a library path, and it has an MPN's shape.

    Fed to the parser it becomes `mpn_norm='C06031608METRIC'`, which invents a
    part number for the line and pushes it through FTS as a filter — so the line
    matches nothing at all while looking perfectly well understood. The footprint
    column is therefore excluded from the text a line is read from, and that is
    what this pins.
    """
    project_id, _line_id, _part_id = project_with_a_line

    (answer,) = client.get(f"/api/projects/{project_id}/bom/suggestions").json()["lines"]

    assert answer["requirement"]["mpn"] is None
    assert answer["text"] == "100nF CAP CER 25V ceramic"
    assert answer["outcome"] == "stocked"


def test_required_milli_scales_with_the_assembly_count(
    project_with_a_line: tuple[int, int, int], client: TestClient
) -> None:
    """Demand is derived, never stored — and this route has no build in scope.

    Three units per assembly against 500 in the drawer: fine for one board,
    short for a thousand. Nothing is written either way, which is what makes
    `assembly_count` a query parameter rather than a state change.
    """
    project_id, _line_id, _part_id = project_with_a_line

    one = client.get(f"/api/projects/{project_id}/bom/suggestions").json()["lines"][0]
    assert one["required_milli"] == 3_000
    assert one["in_stock"][0]["covers_required"] is True

    many = client.get(
        f"/api/projects/{project_id}/bom/suggestions", params={"assembly_count": 1000}
    ).json()["lines"][0]
    assert many["required_milli"] == 3_000_000
    assert many["in_stock"][0]["covers_required"] is False


def test_accepting_a_suggestion_writes_an_ordinary_bom_line(
    project_with_a_line: tuple[int, int, int], client: TestClient
) -> None:
    """No accept endpoint: the existing BOM edit *is* the accept.

    `PUT /api/projects/{id}/bom` already owns the one code path that writes
    `bom_lines`, including the rule that a human setting `part_id` through it is
    the confirmation an automatic exact-MPN hit is not. A dedicated accept route
    would have to restate that rule beside it.
    """
    project_id, line_id, part_id = project_with_a_line

    (answer,) = client.get(f"/api/projects/{project_id}/bom/suggestions").json()["lines"]
    chosen = answer["in_stock"][0]["part_id"]

    accepted = client.put(
        f"/api/projects/{project_id}/bom",
        json={"edits": [{"id": line_id, "part_id": chosen}]},
    )
    assert accepted.status_code == 200, accepted.text
    (line,) = accepted.json()["lines"]
    assert line["part_id"] == part_id
    assert line["is_match_confirmed"] is True
    assert line["description"] == "CAP CER 25V ceramic"

    # And the line drops out of the worklist, because it now has a part.
    assert client.get(f"/api/projects/{project_id}/bom/suggestions").json()["total"] == 0


def test_rejecting_leaves_the_line_unmatched_with_its_description_intact(
    project_with_a_line: tuple[int, int, int], client: TestClient
) -> None:
    """Rejecting is calling nothing — and undoing an acceptance keeps the text.

    Both halves matter. Asking for suggestions must write nothing, so a user who
    looks and walks away has changed no state; and clearing a `part_id` that was
    accepted by mistake has to leave the description behind, because the
    description is the only thing that will ever let the line be matched again.
    """
    project_id, line_id, part_id = project_with_a_line

    client.get(f"/api/projects/{project_id}/bom/suggestions")
    (before,) = client.get(f"/api/projects/{project_id}/bom").json()["lines"]
    assert before["part_id"] is None
    assert before["is_match_confirmed"] is False
    assert before["description"] == "CAP CER 25V ceramic"

    client.put(
        f"/api/projects/{project_id}/bom",
        json={"edits": [{"id": line_id, "part_id": part_id}]},
    )
    rejected = client.put(
        f"/api/projects/{project_id}/bom",
        json={"edits": [{"id": line_id, "part_id": None}]},
    )
    assert rejected.status_code == 200, rejected.text
    (line,) = rejected.json()["lines"]
    assert line["part_id"] is None
    assert line["is_match_confirmed"] is False
    assert line["description"] == "CAP CER 25V ceramic"
    assert line["ref_value"] == "100nF"
