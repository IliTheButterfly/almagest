"""`routes.py` and the fields the tools use, against the committed `openapi.json`.

`CLAUDE.md`: "API clients are **generated** from FastAPI's OpenAPI schema, never
hand-written — that is what makes the cross-repo splits safe." This client is
hand-written, so this file is the compensating control, exactly as
`deviceagent/tests/test_api_contract.py` is for the agent's.

Two layers, because they catch different mistakes:

* **Routes** — every entry in `ROUTES` is a real operation at that exact method
  and path, matched by operation id. Catches a moved path or a renamed handler.
* **Fields** — the request fields the tools send and the response fields they read
  exist. Catches the rename that leaves the route intact and the tool silently
  returning `None` for the number a person asked for.

`make openapi` regenerates the schema and CI fails if it is stale, so what is read
here is what the running API serves.
"""

from __future__ import annotations

from typing import Any

import pytest

from almagest_mcp.routes import ROUTES
from almagest_mcp.tools import LEDGER_SOURCE, QTY_MILLI_MAX


def deref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    resolved: Any = schema
    for part in ref.lstrip("#/").split("/"):
        resolved = resolved[part]
    return deref(schema, resolved)


def properties(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """The property map of a schema node, following `$ref`, `anyOf` and arrays.

    `anyOf` is how Pydantic renders `X | None`, so a nullable field's real shape is
    in one of the branches; `items` is how it renders `list[X]`, which is what most
    of these responses are wrapped in.
    """
    node = deref(schema, node)
    found = node.get("properties")
    if isinstance(found, dict):
        return found
    if "items" in node:
        return properties(schema, node["items"])
    for branch in (*node.get("anyOf", []), *node.get("allOf", [])):
        nested = properties(schema, branch)
        if nested:
            return nested
    return {}


def response_properties(
    schema: dict[str, Any],
    operations: dict[str, tuple[str, str, dict[str, Any]]],
    operation_id: str,
) -> dict[str, Any]:
    _, _, operation = operations[operation_id]
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    return properties(schema, ok)


def request_properties(
    schema: dict[str, Any],
    operations: dict[str, tuple[str, str, dict[str, Any]]],
    operation_id: str,
) -> dict[str, Any]:
    _, _, operation = operations[operation_id]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    return properties(schema, body)


def query_names(
    operations: dict[str, tuple[str, str, dict[str, Any]]], operation_id: str
) -> set[str]:
    _, _, operation = operations[operation_id]
    return {
        parameter["name"]
        for parameter in operation.get("parameters", [])
        if parameter.get("in") == "query"
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation_id", sorted(ROUTES))
def test_every_route_matches_the_schema(
    operation_id: str, operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """Parametrized so a failure names the one operation that moved."""
    assert operation_id in operations, (
        f"{operation_id} is not an operation id in openapi.json. The operation id is "
        "the handler's function name (backend/app/main.py); if the handler was "
        "renamed, rename it here and in coverage.py."
    )
    method, path, _ = operations[operation_id]
    route = ROUTES[operation_id]
    assert (route.method, route.path) == (method, path), (
        f"{operation_id} is {method.upper()} {path} in openapi.json, but routes.py "
        f"has {route.method.upper()} {route.path}"
    )


# ---------------------------------------------------------------------------
# Search — the field names the search tools build a body from
# ---------------------------------------------------------------------------


def test_search_takes_the_narrowing_fields_the_tool_sends(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    sent = request_properties(schema, operations, "search_parts")
    assert {
        "text",
        "category",
        "filters",
        "in_stock_only",
        "mode",
        "include_stubs",
        "limit",
        "offset",
    } <= set(sent)


def test_a_filter_is_a_template_and_a_value(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """`_filters_to_list` builds exactly these two keys from its mapping."""
    sent = request_properties(schema, operations, "search_parts")
    assert {"template", "value"} <= set(properties(schema, sent["filters"]))


def test_search_results_carry_what_the_tool_promises(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """The tool's docstring tells a model to read these three to judge stock."""
    read = response_properties(schema, operations, "search_parts")
    assert {"total", "results"} <= set(read)
    hit = properties(schema, read["results"])
    assert {"id", "name", "mpn", "qty_milli", "location_count", "is_stub"} <= set(hit)


def test_the_facets_route_takes_the_two_narrowing_fields_the_tool_sends(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    sent = request_properties(schema, operations, "parameter_facets")
    assert {"category", "part_kind"} <= set(sent)


# ---------------------------------------------------------------------------
# Parts, lots, locations — "where is it" and "how many"
# ---------------------------------------------------------------------------


def test_a_part_carries_its_lots(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """`get_part` is the answer to "where is it", and that answer is `lots`."""
    read = response_properties(schema, operations, "read_part")
    assert {"id", "name", "mpn", "short_id", "total_qty_milli", "lots"} <= set(read)
    lot = properties(schema, read["lots"])
    assert {
        "id",
        "part_id",
        "location_id",
        "location_label_path",
        "qty_milli",
        "qty_reserved_milli",
    } <= set(lot)


def test_a_location_carries_its_path_and_contents(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    read = response_properties(schema, operations, "read_location")
    assert {"id", "name", "label_path", "short_id", "lots", "is_overfull"} <= set(read)


def test_a_location_carries_the_millimetres_get_location_promises(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """`get_location`'s docstring tells a model to answer "does this fit" from
    these exact names, against a part's own dimensions. The container-type routes
    that also hold them are excluded from the tool surface as authoring, so this
    is the *only* door to them — a rename here would leave the docstring
    instructing a model to read fields that are silently absent.
    """
    read = response_properties(schema, operations, "read_location")
    geometry = properties(schema, read["geometry"])
    assert {
        "container_type_slug",
        "container_type_display_name",
        "inner_length_mm",
        "inner_width_mm",
        "inner_height_mm",
        "inner_volume_mm3",
        "max_item_dimension_mm",
        "fill_factor",
        "allowed_part_kinds",
    } <= set(geometry)


def test_a_part_carries_the_dimensions_to_compare_against_a_container(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """The other half of the fit question, and `volume_source` with it: `get_part`
    tells a model to read that before quoting a volume as measured rather than
    estimated."""
    read = response_properties(schema, operations, "read_part")
    assert {
        "length_mm",
        "width_mm",
        "height_mm",
        "unit_volume_mm3",
        "unit_mass_mg",
        "volume_source",
    } <= set(read)


def test_the_tree_carries_structure_without_recursion(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """`browse_locations` promises a flat list a caller can render from."""
    read = response_properties(schema, operations, "read_location_tree")
    node = properties(schema, read["nodes"])
    assert {"id", "parent_id", "label_path", "depth", "lot_count", "qty_milli"} <= set(node)
    assert {"root_id", "include_retired"} <= query_names(operations, "read_location_tree")


def test_lot_history_carries_the_append_only_evidence(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """`reversal_of_seq` and `source` are both quoted in the tool's docstring."""
    read = response_properties(schema, operations, "read_lot_history")
    assert {"seq", "delta_milli", "qty_after_milli", "reversal_of_seq", "source", "kind"} <= set(
        read
    )
    assert "limit" in query_names(operations, "read_lot_history")


# ---------------------------------------------------------------------------
# Requirements and BOMs — the proposal surfaces
# ---------------------------------------------------------------------------


def test_a_requirement_line_is_free_text(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    sent = request_properties(schema, operations, "suggest_parts")
    assert {"lines", "limit"} <= set(sent)
    assert "text" in properties(schema, sent["lines"])


def test_a_suggestion_shows_what_it_understood_and_what_it_did_not(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """The tool tells a model to read `requirement` and `residue` before trusting
    candidates. Both have to be there for that instruction to mean anything."""
    read = response_properties(schema, operations, "suggest_parts")
    line = properties(schema, read["lines"])
    assert {"requirement", "in_stock", "not_stocked", "outcome", "message"} <= set(line)
    requirement = properties(schema, line["requirement"])
    assert {"residue", "is_actionable", "confidence"} <= set(requirement)


def test_bom_suggestions_take_the_query_the_tool_sends(
    operations: dict[str, tuple[str, str, dict[str, Any]]],
) -> None:
    assert {
        "unmatched_only",
        "assembly_count",
        "limit",
        "offset",
        "candidates",
    } <= query_names(operations, "read_bom_suggestions")


def test_a_bom_line_says_whether_a_human_confirmed_the_match(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    read = response_properties(schema, operations, "list_bom_lines")
    line = properties(schema, read["lines"])
    assert {"line_no", "part_id", "is_match_confirmed", "qty_per_assembly_milli"} <= set(line)


# ---------------------------------------------------------------------------
# Builds
# ---------------------------------------------------------------------------


def test_shortages_answer_can_i_build_this(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    read = response_properties(schema, operations, "read_shortages")
    assert {"build_id", "is_buildable", "assembly_count", "lines"} <= set(read)
    line = properties(schema, read["lines"])
    assert {"required_milli", "shortfall_milli", "is_blocking", "substitute_part_ids"} <= set(line)


def test_a_pick_list_never_hides_its_gaps(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """The tool's docstring instructs the model to report `gaps` whenever
    `is_complete` is false, which is only honest if the API keeps sending them."""
    read = response_properties(schema, operations, "read_pick_list")
    assert {"build_id", "is_complete", "stops", "gaps"} <= set(read)
    stop = properties(schema, read["stops"])
    assert {"location_id", "label_path", "qty_milli"} <= set(stop)


# ---------------------------------------------------------------------------
# Movements — the write surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("operation_id", ["consume_stock", "return_stock"])
def test_a_quantity_movement_takes_the_envelope_and_a_quantity(
    operation_id: str,
    schema: dict[str, Any],
    operations: dict[str, tuple[str, str, dict[str, Any]]],
) -> None:
    sent = request_properties(schema, operations, operation_id)
    assert {"qty_milli", "client_op_id", "device_id", "source", "note"} <= set(sent)


def test_move_takes_a_destination_and_an_optional_split(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    sent = request_properties(schema, operations, "move_stock")
    assert {"to_location_id", "qty_milli", "client_op_id"} <= set(sent)


def test_recount_sets_a_counted_balance(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """A different field name from the other three on purpose — it is not a delta,
    and `recount_stock` converts into it separately."""
    sent = request_properties(schema, operations, "recount_stock")
    assert {"counted_qty_milli", "client_op_id"} <= set(sent)


def test_undo_accepts_both_handles_the_tool_offers(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    sent = request_properties(schema, operations, "undo_movement")
    assert {"client_op_id_to_undo", "seq"} <= set(sent)


def test_a_movement_response_carries_the_lot_and_its_replay_flag(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """`_movement_result` reads exactly these, and reports `replayed` to the model
    because "already recorded" and "recorded" are different facts."""
    read = response_properties(schema, operations, "consume_stock")
    assert {"lot", "seqs", "replayed", "group_uuid", "counterpart_lot"} <= set(read)


def test_api_is_a_real_ledger_source(schema: dict[str, Any]) -> None:
    """`LEDGER_SOURCE` is a bare string on the wire, so nothing else checks it.

    A value the API's enum does not carry would be a 422 on every write, and only
    on writes — which are off by default, so it could ship unnoticed.
    """
    assert LEDGER_SOURCE in schema["components"]["schemas"]["LedgerSource"]["enum"]


def test_the_quantity_ceiling_still_matches_the_api(
    schema: dict[str, Any], operations: dict[str, tuple[str, str, dict[str, Any]]]
) -> None:
    """`units_to_milli` refuses above `QTY_MILLI_MAX` so an over-limit request never
    leaves. A widened bound backend-side should be picked up here, not guessed at."""
    sent = request_properties(schema, operations, "consume_stock")
    assert sent["qty_milli"]["maximum"] == QTY_MILLI_MAX
