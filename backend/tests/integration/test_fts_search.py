"""FTS5 ranking, against a real index.

The unit tests prove the query language cannot be broken. These prove the other
half: that the index is actually populated, actually consulted, and that ranking
happens *within* the filtered set rather than instead of filtering.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.catalog import Part, PartKind
from app.models.parameter import ParameterTemplate
from app.scripts.seed_demo import seed_catalogue
from app.services import parameters
from app.services.search.fts import build_param_digest, rebuild_all_param_digests
from app.services.search.query_builder import Filter, SearchQuery, execute


@pytest.fixture
def catalogue(db: Session) -> Session:
    seed_catalogue(db)
    db.commit()
    return db


def _add_part(db: Session, name: str, **fields: object) -> Part:
    kind = db.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()
    part = Part(name=name, part_kind_id=kind.id, **fields)
    db.add(part)
    db.flush()
    return part


def _mpns(parts: list[Part]) -> list[str]:
    return [part.mpn or part.name for part in parts]


# ---------------------------------------------------------------------------
# The index is real
# ---------------------------------------------------------------------------


def test_the_index_is_populated_by_the_triggers(catalogue: Session) -> None:
    """`part_fts` is kept current by triggers on `parts`, so a part inserted by
    ordinary ORM code must be findable with no extra step."""
    _add_part(catalogue, "very distinctive widget", mpn="WIDGET-1")
    catalogue.commit()

    found = execute(catalogue, SearchQuery(text="distinctive"))
    assert _mpns(found) == ["WIDGET-1"]


def test_an_update_reindexes(catalogue: Session) -> None:
    part = _add_part(catalogue, "originalname", mpn="UPD-1")
    catalogue.commit()

    part.description = "afterwards mentioning tantalum"
    catalogue.commit()

    assert _mpns(execute(catalogue, SearchQuery(text="tantalum"))) == ["UPD-1"]


def test_a_delete_removes_it_from_the_index(catalogue: Session) -> None:
    part = _add_part(catalogue, "ephemeral gizmo", mpn="DEL-1")
    catalogue.commit()
    assert execute(catalogue, SearchQuery(text="gizmo"))

    catalogue.delete(part)
    catalogue.commit()
    assert execute(catalogue, SearchQuery(text="gizmo")) == []


def test_a_part_with_only_a_name_is_findable(catalogue: Session) -> None:
    """The intake fast path produces exactly this: a row with a name and nothing
    else. If the index skipped `name`, a scanned-in stub would be unfindable by
    the only words it has."""
    _add_part(catalogue, "unidentified salvage thingamajig")
    catalogue.commit()

    assert len(execute(catalogue, SearchQuery(text="thingamajig"))) == 1


# ---------------------------------------------------------------------------
# Filter first, then rank
# ---------------------------------------------------------------------------


def test_a_text_search_composes_with_parametric_filters(catalogue: Session) -> None:
    """The composition that matters: the filters narrow, and FTS only ranks what
    survives. If FTS replaced filtering, this would return the electrolytic too."""
    found = execute(
        catalogue,
        SearchQuery(
            text="ceramic",
            filters=(Filter("mounting_type", "THT"), Filter("capacitance", "20-30uF")),
        ),
    )
    assert _mpns(found) == ["DEMO-CAP-THT-22U"]


def test_a_filter_that_excludes_everything_wins_over_a_matching_text(
    catalogue: Session,
) -> None:
    """Text is not allowed to smuggle a part past a parametric predicate."""
    found = execute(
        catalogue,
        SearchQuery(text="ceramic", filters=(Filter("capacitance", "900-1000uF"),)),
    )
    assert found == []


def test_a_better_lexical_match_ranks_higher(catalogue: Session) -> None:
    """Ranking has to actually order, not just filter."""
    _add_part(catalogue, "zzz mentions ferrite once", mpn="RANK-WEAK", description="ferrite")
    _add_part(
        catalogue,
        "aaa ferrite ferrite ferrite",
        mpn="RANK-STRONG",
        description="ferrite ferrite ferrite bead ferrite",
        keywords="ferrite",
    )
    catalogue.commit()

    ranked = _mpns(execute(catalogue, SearchQuery(text="ferrite")))
    # Note the names are deliberately reverse-alphabetical to the desired order,
    # so an alphabetical fallback would fail this.
    assert ranked[0] == "RANK-STRONG"


def test_an_mpn_hit_outranks_a_description_mention(catalogue: Session) -> None:
    """Somebody typing an MPN wants that part, not every part whose description
    happens to mention it."""
    _add_part(catalogue, "aaa a part that mentions", mpn="OTHER-1", description="XR2206 compatible")
    _add_part(catalogue, "zzz the actual chip", mpn="XR2206")
    catalogue.commit()

    ranked = _mpns(execute(catalogue, SearchQuery(text="XR2206")))
    assert ranked[0] == "XR2206"


def test_ordering_stays_deterministic_under_ranking(catalogue: Session) -> None:
    """Equal-ranking rows must not reshuffle between pages, or pagination drops
    and repeats rows."""
    for index in range(6):
        _add_part(catalogue, f"identical widget {index}", mpn=f"TIE-{index}", description="widget")
    catalogue.commit()

    first = _mpns(execute(catalogue, SearchQuery(text="widget", limit=100)))
    for _ in range(4):
        assert _mpns(execute(catalogue, SearchQuery(text="widget", limit=100))) == first


def test_pagination_under_ranking_does_not_repeat_rows(catalogue: Session) -> None:
    for index in range(6):
        _add_part(catalogue, f"paged widget {index}", mpn=f"PAGE-{index}", description="widget")
    catalogue.commit()

    page_one = _mpns(execute(catalogue, SearchQuery(text="widget", limit=3, offset=0)))
    page_two = _mpns(execute(catalogue, SearchQuery(text="widget", limit=3, offset=3)))
    assert len(page_one) == 3
    assert not set(page_one) & set(page_two)


# ---------------------------------------------------------------------------
# No text: skip FTS entirely
# ---------------------------------------------------------------------------


def test_without_a_text_term_stocked_parts_come_first(catalogue: Session) -> None:
    """The specified fallback ordering. A part you actually have is nearly always
    the one being looked for."""
    from tests.factories import make_location, make_lot

    parts = list(catalogue.execute(select(Part).order_by(Part.name)).scalars())
    stocked = parts[-1]  # deliberately last alphabetically
    make_lot(catalogue, stocked, make_location(catalogue), qty_milli=1000)
    catalogue.commit()

    ordered = execute(catalogue, SearchQuery())
    assert ordered[0].id == stocked.id


def test_punctuation_only_text_finds_nothing_rather_than_everything(
    catalogue: Session,
) -> None:
    """A user who typed something and got the whole catalogue would reasonably
    conclude search is broken."""
    assert execute(catalogue, SearchQuery(text="!!!")) == []


# ---------------------------------------------------------------------------
# param_digest
# ---------------------------------------------------------------------------


def test_a_parameter_value_becomes_findable_as_free_text(catalogue: Session) -> None:
    """The reason param_digest exists: "10k 0603" should work without the user
    constructing a parametric query."""
    rebuild_all_param_digests(catalogue)
    catalogue.commit()

    found = _mpns(execute(catalogue, SearchQuery(text="0603")))
    assert "DEMO-RES-10K" in found


def test_the_digest_carries_both_the_display_form_and_the_raw_input(
    catalogue: Session,
) -> None:
    """So a part filed as "4k7" is findable by typing "4k7" *or* "4.7 kΩ"."""
    part = catalogue.execute(select(Part).where(Part.mpn == "DEMO-RES-4K7")).scalar_one()
    digest = build_param_digest(catalogue, part.id)

    assert "4k7" in digest
    assert "4.7k" in digest.replace(" ", "")


def test_the_digest_is_refreshed_by_the_parameter_write_path(catalogue: Session) -> None:
    """A stale digest is a part that silently stops being findable by its own
    value, so the write path has to own it."""
    part = _add_part(catalogue, "freshly parameterised", mpn="DIGEST-1")
    template = catalogue.execute(
        select(ParameterTemplate).where(ParameterTemplate.name == "resistance")
    ).scalar_one()

    parameters.set_numeric(catalogue, part, template, "22k")
    catalogue.commit()

    stored = catalogue.execute(
        text("SELECT param_digest FROM part_fts WHERE rowid = :id"), {"id": part.id}
    ).scalar_one()
    assert "22k" in (stored or "")

    assert "DIGEST-1" in _mpns(execute(catalogue, SearchQuery(text="22k")))


def test_changing_a_value_updates_the_digest(catalogue: Session) -> None:
    part = _add_part(catalogue, "revised part", mpn="DIGEST-2")
    template = catalogue.execute(
        select(ParameterTemplate).where(ParameterTemplate.name == "resistance")
    ).scalar_one()

    parameters.set_numeric(catalogue, part, template, "1k")
    catalogue.commit()
    assert "DIGEST-2" in _mpns(execute(catalogue, SearchQuery(text="1k")))

    parameters.set_numeric(catalogue, part, template, "47k")
    catalogue.commit()

    assert "DIGEST-2" in _mpns(execute(catalogue, SearchQuery(text="47k")))
    # And no longer findable by the value it used to have.
    assert "DIGEST-2" not in _mpns(execute(catalogue, SearchQuery(text="1k")))


# ---------------------------------------------------------------------------
# Hostile input, end to end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ['" OR 1=1 --', "mpn:secret", "^anchor", "a NEAR b", "(unbalanced", "***", "resistor*"],
)
def test_hostile_search_text_never_errors_over_http(client: TestClient, hostile: str) -> None:
    """A stray quote in a search box must not produce a 500. This is the failure
    the allowlist exists to prevent, verified against real SQLite rather than
    only against the query builder."""
    response = client.get("/api/search/parts", params={"text": hostile})
    assert response.status_code == 200, response.text
    assert "results" in response.json()


def test_hostile_text_combined_with_filters_is_also_safe(client: TestClient) -> None:
    """The two sanitisation paths meet here — the FTS allowlist and the value
    parser — so it is worth proving they compose rather than fight."""
    session = get_session_factory()()
    try:
        seed_catalogue(session)
        session.commit()
    finally:
        session.close()

    response = client.post(
        "/api/search/parts",
        json={"text": '"" OR 1=1', "filters": [{"template": "resistance", "value": "4k7"}]},
    )
    assert response.status_code == 200, response.text
