"""Quantities an install defines itself, and then filters by.

Iliana: "add more unit types. like lumen and also add custom units".

The shipped half is a data change and is covered in the library's own suite. What
needs testing here is the custom half, and the thing worth testing is **not** that
a row can be written — it is that a field measured in a user-defined unit behaves
exactly like one measured in farads, all the way through to a search that finds a
part by it. A quantity that stores fine and then silently matches nothing is the
failure this whole path exists to prevent, and it looks identical to success from
the authoring screen.

So the headline test defines a quantity, authors a field in it, stores a value
through the same funnel every writer uses, and searches for the part by a range.
The rest are the ways it goes wrong, each silent by default:

* a name the library already answers to (`farad`, or its alias `resistance`) —
  would redefine what every stored value of that quantity means, without touching
  a row;
* a symbol the grammar cannot read a value under — accepted, then every value
  refused forever, field permanently empty;
* an inverted window — a field no value can fall in;
* a definition deleted out from under the fields measured in it — every one of
  them stops reading values, and their stored numbers lose their unit.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from elec_value_parser import ImplausibleValueError, registered_quantities, unregister_quantity
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_session_factory
from app.models.catalog import Part
from app.models.parameter import ParameterTemplate
from app.scripts.seed_demo import seed_all
from app.services import parameters
from app.services.search.value_parser import forget_quantity_cache


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Unregister anything a test defined.

    The parser's runtime registry is **process-wide module state** — that is the
    deliberate design (the table is the source of truth, the registry is a
    per-process view), and it means a quantity registered by one test would
    otherwise still be registered for the next one, which would make a test that
    asserts "this name is unknown" pass or fail depending on what ran before it.
    """
    yield
    for quantity in registered_quantities():
        unregister_quantity(quantity.name)
    forget_quantity_cache()


def _session() -> Session:
    return get_session_factory()()


def _seed() -> None:
    session = _session()
    try:
        seed_all(session)
        session.commit()
    finally:
        session.close()


def _quantity(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/parameter-quantities", json=body)
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()["quantity"]
    return payload


def _set_numeric(part_id: int, template_name: str, raw: str) -> None:
    """Store a value the only sanctioned way — `app.services.parameters`, which is
    what guarantees `value_min`/`value_max` and therefore that search can see it."""
    session = _session()
    try:
        part = session.get(Part, part_id)
        assert part is not None
        template = session.execute(
            select(ParameterTemplate).where(ParameterTemplate.name == template_name)
        ).scalar_one()
        parameters.set_numeric(session, part, template, raw)
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The headline: define a unit, then find a part by it
# ---------------------------------------------------------------------------


def test_a_custom_unit_becomes_a_working_filter(client: TestClient) -> None:
    _seed()
    _quantity(
        client,
        name="byte",
        symbol="B",
        display_name="Bytes",
        word_aliases=["byte", "bytes"],
        low=1,
        high=1e15,
    )

    field = client.post(
        "/api/parameter-fields",
        json={
            "name": "flash_size",
            "display_name": "Flash",
            "value_type": "numeric",
            "base_unit": "byte",
            "substitution_direction": "higher_ok",
        },
    )
    assert field.status_code == 201, field.text
    assert field.json()["field"]["base_unit"] == "byte"

    part = client.post("/api/parts", json={"name": "STM32F103C8T6", "part_kind": "component"})
    assert part.status_code == 201, part.text
    part_id = int(part.json()["part"]["id"])
    # `64kB` exercises the prefix on a custom symbol, which is the whole point of
    # the definition carrying `allow_prefix`.
    _set_numeric(part_id, "flash_size", "64kB")

    found = client.post(
        "/api/search/parts",
        json={"filters": [{"template": "flash_size", "value": "32kB-128kB"}]},
    )
    assert found.status_code == 200, found.text
    assert [row["id"] for row in found.json()["results"]] == [part_id]

    # And a range that excludes it really excludes it — otherwise the assertion
    # above would pass for a filter that matches everything.
    missed = client.post(
        "/api/search/parts",
        json={"filters": [{"template": "flash_size", "value": "256kB-1MB"}]},
    )
    assert missed.status_code == 200, missed.text
    assert missed.json()["results"] == []


def test_a_shipped_quantity_added_this_round_is_namable(client: TestClient) -> None:
    """The other half of the ask: `lumen` and friends, with no custom row at all."""
    _seed()
    field = client.post(
        "/api/parameter-fields",
        json={
            "name": "luminous_flux",
            "display_name": "Brightness",
            "value_type": "numeric",
            "base_unit": "lumen",
            "substitution_direction": "higher_ok",
        },
    )
    assert field.status_code == 201, field.text

    part = client.post("/api/parts", json={"name": "CREE XPG3", "part_kind": "component"})
    part_id = int(part.json()["part"]["id"])
    _set_numeric(part_id, "luminous_flux", "1200lm")

    found = client.post(
        "/api/search/parts",
        json={"filters": [{"template": "luminous_flux", "value": ">=1000lm"}]},
    )
    assert [row["id"] for row in found.json()["results"]] == [part_id]


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["farad", "resistance", "OHM"])
def test_a_shipped_quantity_cannot_be_redefined(client: TestClient, name: str) -> None:
    """Including by an alias and regardless of case.

    `resistance` resolves to `ohm` for every existing caller, so letting a row take
    that name would change what an already-written `base_unit` string means.
    """
    response = client.post(
        "/api/parameter-quantities",
        json={"name": name, "symbol": "X", "display_name": "Mine"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["reason"] == "builtin_quantity"


def test_a_duplicate_name_is_a_clean_conflict(client: TestClient) -> None:
    _quantity(client, name="turn", symbol="turns", display_name="Turns", allow_prefix=False)
    again = client.post(
        "/api/parameter-quantities",
        json={"name": "turn", "symbol": "t", "display_name": "Turns again"},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "duplicate_quantity"


def test_an_unreadable_symbol_is_refused_at_authoring_time(client: TestClient) -> None:
    """A symbol the grammar cannot read is the silent case: the row stores, the
    field is offered, and every value anyone types is refused from then on.

    A digit in a symbol does it — the tokenizer has already taken the digits as the
    mantissa by the time it reaches the unit.
    """
    response = client.post(
        "/api/parameter-quantities",
        json={"name": "weird", "symbol": "1x", "display_name": "Weird"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["reason"] == "unparseable_symbol"


def test_an_inverted_window_is_refused(client: TestClient) -> None:
    response = client.post(
        "/api/parameter-quantities",
        json={"name": "runtime", "symbol": "h", "display_name": "Hours", "low": 10, "high": 1},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "inverted_plausibility"


def test_the_quantitys_own_window_refuses_an_implausible_value(client: TestClient) -> None:
    """The window is the parser-level guard, independent of a field's own — so it
    applies to every field measured in the unit without being restated."""
    _seed()
    _quantity(
        client,
        name="turn",
        symbol="turns",
        display_name="Turns",
        low=1,
        high=1e4,
        allow_prefix=False,
    )
    client.post(
        "/api/parameter-fields",
        json={
            "name": "winding_turns",
            "display_name": "Turns",
            "value_type": "numeric",
            "base_unit": "turn",
            "substitution_direction": "exact",
        },
    )
    part = client.post("/api/parts", json={"name": "Toroid", "part_kind": "component"})
    part_id = int(part.json()["part"]["id"])
    # Named rather than blind: the quantity's own window is what refuses this, and
    # a different error here would mean something else went wrong.
    with pytest.raises(ImplausibleValueError):
        _set_numeric(part_id, "winding_turns", "0.2turns")


# ---------------------------------------------------------------------------
# Deleting one
# ---------------------------------------------------------------------------


def test_a_quantity_in_use_cannot_be_deleted(client: TestClient) -> None:
    _seed()
    quantity = _quantity(client, name="byte", symbol="B", display_name="Bytes", low=1, high=1e15)
    client.post(
        "/api/parameter-fields",
        json={
            "name": "flash_size",
            "display_name": "Flash",
            "value_type": "numeric",
            "base_unit": "byte",
            "substitution_direction": "higher_ok",
        },
    )

    refused = client.delete(f"/api/parameter-quantities/{quantity['id']}")
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "quantity_in_use"
    assert "1 field" in refused.json()["detail"]["message"]


def test_an_unused_quantity_can_be_deleted_and_stops_being_namable(client: TestClient) -> None:
    quantity = _quantity(client, name="byte", symbol="B", display_name="Bytes")
    listed = client.get("/api/parameter-quantities").json()
    assert any(row["name"] == "byte" and row["custom"] for row in listed)

    removed = client.delete(f"/api/parameter-quantities/{quantity['id']}")
    assert removed.status_code == 200, removed.text

    listed_again = client.get("/api/parameter-quantities").json()
    assert not any(row["name"] == "byte" for row in listed_again)
    # And a field can no longer be authored against it, which is the point of
    # unregistering rather than merely deleting the row.
    field = client.post(
        "/api/parameter-fields",
        json={
            "name": "flash_size",
            "display_name": "Flash",
            "value_type": "numeric",
            "base_unit": "byte",
            "substitution_direction": "higher_ok",
        },
    )
    assert field.status_code == 422
    assert field.json()["detail"]["reason"] == "unknown_base_unit"


# ---------------------------------------------------------------------------
# The picker, and what it reports
# ---------------------------------------------------------------------------


def test_the_picker_lists_shipped_and_custom_together(client: TestClient) -> None:
    _quantity(client, name="byte", symbol="B", display_name="Bytes")
    rows = client.get("/api/parameter-quantities").json()
    by_name = {row["name"]: row for row in rows}

    assert by_name["ohm"]["custom"] is False
    assert by_name["ohm"]["symbol"] == "Ω"
    assert by_name["lumen"]["custom"] is False
    assert by_name["byte"]["custom"] is True

    # `base-units`, which the field form's unit select reads, has to agree — two
    # lists of namable units that could disagree is how a unit gets offered by one
    # screen and refused by the route behind it.
    units = {row["name"] for row in client.get("/api/parameter-fields/base-units").json()}
    assert {"ohm", "lumen", "byte"} <= units


def test_a_custom_quantity_survives_a_restart(client: TestClient) -> None:
    """The registry is per-process and the table is the source of truth, so a fresh
    application has to load them at startup — otherwise every value of every field
    measured in a custom unit fails to parse after a redeploy.
    """
    _quantity(client, name="byte", symbol="B", display_name="Bytes")
    # Drop it from this process's registry, as a restart would.
    unregister_quantity("byte")
    forget_quantity_cache()

    from app.main import app

    with TestClient(app) as restarted:
        units = {row["name"] for row in restarted.get("/api/parameter-fields/base-units").json()}
        assert "byte" in units
