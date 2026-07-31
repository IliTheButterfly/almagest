"""What the tools actually send, and what they hand back.

The translation is the work here — units in and out, a filter mapping becoming the
API's list, a movement carrying an idempotency key — so it is the translation that
is tested. Tools are driven through `call_tool` rather than called directly, so the
argument schemas the protocol advertises are exercised too: a `Field` constraint
that does not hold is a failure here, not a surprise at a model's first call.

`test_api_contract.py` covers the other half — that the fields these requests use
exist in the API. Neither test needs a running server, which is why both run in
`make check` in under two seconds.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# `tests/` is not a package, so pytest puts this directory on `sys.path` and the
# fixture module is importable by name.
from conftest import ScriptedTransport
from mcp.server.mcpserver.exceptions import ToolError

from almagest_mcp.config import McpSettings
from almagest_mcp.server import build_server
from almagest_mcp.tools import units_to_milli


def call(
    tool: str,
    arguments: dict[str, Any],
    *,
    responses: dict[tuple[str, str], Any] | None = None,
    allow_writes: bool = True,
) -> tuple[dict[str, Any], ScriptedTransport]:
    """Invoke one tool through the protocol surface and return its parsed result."""
    transport = ScriptedTransport(responses)
    settings = McpSettings(ALMAGEST_MCP_ALLOW_WRITES=allow_writes)  # type: ignore[call-arg]
    server = build_server(settings=settings, transport=transport)
    result = asyncio.run(server.call_tool(tool, arguments))
    assert not result.is_error, result.content
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert isinstance(payload, dict)
    return payload, transport


def call_expecting_error(tool: str, arguments: dict[str, Any]) -> str:
    """Drive a tool that should refuse, and return the message a model would see.

    The SDK turns a raised exception into `ToolError` on the way out, so a refusal
    is caught rather than read off the result — and the message is what a model gets
    handed, which is the thing worth asserting on.
    """
    transport = ScriptedTransport()
    settings = McpSettings(ALMAGEST_MCP_ALLOW_WRITES=True)  # type: ignore[call-arg]
    server = build_server(settings=settings, transport=transport)
    with pytest.raises(ToolError) as raised:
        asyncio.run(server.call_tool(tool, arguments))
    return str(raised.value)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [(1, 1000), (2.5, 2500), (0.001, 1), (1234, 1234000)],
)
def test_units_become_thousandths(quantity: float, expected: int) -> None:
    assert units_to_milli(quantity) == expected


def test_a_quantity_finer_than_a_thousandth_is_refused_not_rounded() -> None:
    """Rounding it would report a successful movement that moved nothing."""
    with pytest.raises(ValueError, match="finer than a thousandth"):
        units_to_milli(0.0001)


def test_a_quantity_above_the_api_ceiling_never_leaves() -> None:
    with pytest.raises(ValueError, match="exceeds the maximum"):
        units_to_milli(10**10)


def test_read_results_carry_both_denominations() -> None:
    """`qty_milli` stays for reconciliation; `qty` is what gets quoted to a person."""
    payload, _ = call(
        "get_lot",
        {"lot_id": 4},
        responses={
            ("get", "/api/stock/lots/4"): {
                "id": 4,
                "part_id": 7,
                "location_id": 2,
                "qty_milli": 2500,
                "qty_reserved_milli": 500,
                "status": "active",
            }
        },
    )
    assert payload["qty_milli"] == 2500
    assert payload["qty"] == 2.5
    assert payload["qty_reserved"] == 0.5


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_a_filter_mapping_becomes_the_apis_template_value_pairs() -> None:
    _, transport = call(
        "search_parts",
        {"text": "cap", "filters": {"capacitance": "20-30uF", "dielectric": "X7R"}},
        responses={("post", "/api/search/parts"): {"total": 0, "results": []}},
    )
    assert transport.last["body"]["filters"] == [
        {"template": "capacitance", "value": "20-30uF"},
        {"template": "dielectric", "value": "X7R"},
    ]


def test_substitute_mode_is_passed_through_untranslated() -> None:
    """The substitution rule lives in the API's filter executor, decided by each
    field's `substitution_direction`. This server must not reinterpret it."""
    _, transport = call(
        "search_parts",
        {"text": "50V cap", "mode": "substitute"},
        responses={("post", "/api/search/parts"): {"total": 0, "results": []}},
    )
    assert transport.last["body"]["mode"] == "substitute"


def test_an_unknown_search_mode_is_refused_by_the_schema() -> None:
    assert "search" in call_expecting_error("search_parts", {"mode": "guess"}).lower()


def test_search_hits_are_given_a_readable_quantity() -> None:
    payload, _ = call(
        "search_parts",
        {"text": "100nF"},
        responses={
            ("post", "/api/search/parts"): {
                "total": 1,
                "results": [{"id": 7, "name": "C0603", "qty_milli": 4000, "lot_count": 2}],
            }
        },
    )
    assert payload["results"][0]["qty"] == 4.0


# ---------------------------------------------------------------------------
# Short ids
# ---------------------------------------------------------------------------


def test_a_short_id_is_folded_before_it_is_looked_up() -> None:
    """Lower case and a missing dash are the same code; `idcodec` says so."""
    _, transport = call(
        "resolve_short_id",
        {"short_id": "4k7t92m8"},
        responses={("get", "/api/resolve/4K7T92M8"): {"status": "ok"}},
    )
    assert transport.last["path"] == "/api/resolve/4K7T92M8"


def test_a_mistyped_short_id_is_refused_rather_than_looked_up() -> None:
    """The check symbol exists for this. A 404 would read as "no such container"."""
    message = call_expecting_error("resolve_short_id", {"short_id": "4K7T-92M9"})
    assert "not a valid" in message


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _movement_response() -> dict[str, Any]:
    return {
        "lot": {"id": 4, "part_id": 7, "location_id": 2, "qty_milli": 1500},
        "seqs": [91],
        "replayed": False,
        "group_uuid": None,
        "counterpart_lot": None,
    }


def test_a_consume_sends_units_as_thousandths_with_an_idempotency_key() -> None:
    payload, transport = call(
        "consume_stock",
        {"lot_id": 4, "quantity": 3, "note": "prototype"},
        responses={("post", "/api/stock/lots/4/consume"): _movement_response()},
    )
    body = transport.last["body"]
    assert body["qty_milli"] == 3000
    assert body["source"] == "api"
    assert body["device_id"] == "mcp"
    assert body["note"] == "prototype"
    assert body["client_op_id"]
    # The key is handed back so the model can undo exactly this movement.
    assert payload["client_op_id"] == body["client_op_id"]
    assert payload["ledger_seqs"] == [91]
    assert payload["lot"]["qty"] == 1.5


def test_two_consumes_get_different_keys() -> None:
    """A shared key would make the second call replay the first, silently."""
    first, _ = call(
        "consume_stock",
        {"lot_id": 4, "quantity": 1},
        responses={("post", "/api/stock/lots/4/consume"): _movement_response()},
    )
    second, _ = call(
        "consume_stock",
        {"lot_id": 4, "quantity": 1},
        responses={("post", "/api/stock/lots/4/consume"): _movement_response()},
    )
    assert first["client_op_id"] != second["client_op_id"]


def test_a_replayed_movement_says_so() -> None:
    """`replayed` is the difference between two facts a model must not conflate."""
    replayed = _movement_response() | {"replayed": True}
    payload, _ = call(
        "consume_stock",
        {"lot_id": 4, "quantity": 1},
        responses={("post", "/api/stock/lots/4/consume"): replayed},
    )
    assert payload["replayed"] is True


def test_a_whole_lot_move_sends_no_quantity() -> None:
    """Omitted rather than null: `_without_nones` drops it so the API's own
    "move everything" default applies."""
    _, transport = call(
        "move_stock",
        {"lot_id": 4, "to_location_id": 9},
        responses={("post", "/api/stock/lots/4/move"): _movement_response()},
    )
    assert "qty_milli" not in transport.last["body"]
    assert transport.last["body"]["to_location_id"] == 9


def test_a_partial_move_sends_the_split_quantity() -> None:
    _, transport = call(
        "move_stock",
        {"lot_id": 4, "to_location_id": 9, "quantity": 2.5},
        responses={("post", "/api/stock/lots/4/move"): _movement_response()},
    )
    assert transport.last["body"]["qty_milli"] == 2500


def test_a_recount_sets_a_balance_rather_than_a_delta() -> None:
    _, transport = call(
        "recount_stock",
        {"lot_id": 4, "counted_quantity": 480},
        responses={("post", "/api/stock/lots/4/recount"): _movement_response()},
    )
    assert transport.last["body"]["counted_qty_milli"] == 480_000
    assert "qty_milli" not in transport.last["body"]


def test_a_recount_of_zero_is_allowed() -> None:
    """An empty bin is a real count, and the commonest correction there is."""
    _, transport = call(
        "recount_stock",
        {"lot_id": 4, "counted_quantity": 0},
        responses={("post", "/api/stock/lots/4/recount"): _movement_response()},
    )
    assert transport.last["body"]["counted_qty_milli"] == 0


def test_a_consume_of_zero_is_refused() -> None:
    """Unlike a recount: a movement of nothing is a mistake, not a measurement."""
    call_expecting_error("consume_stock", {"lot_id": 4, "quantity": 0})


def test_undo_requires_exactly_one_handle() -> None:
    assert "exactly one" in call_expecting_error("undo_movement", {})
    assert "exactly one" in call_expecting_error(
        "undo_movement", {"seq": 5, "client_op_id_to_undo": "abc"}
    )


def test_undo_passes_the_handle_it_was_given() -> None:
    _, transport = call(
        "undo_movement",
        {"client_op_id_to_undo": "deadbeef"},
        responses={("post", "/api/stock/undo"): {"seq": 92}},
    )
    body = transport.last["body"]
    assert body["client_op_id_to_undo"] == "deadbeef"
    assert "seq" not in body


def test_a_write_tool_is_unreachable_when_writes_are_off() -> None:
    transport = ScriptedTransport()
    settings = McpSettings(ALMAGEST_MCP_ALLOW_WRITES=False)  # type: ignore[call-arg]
    server = build_server(settings=settings, transport=transport)
    with pytest.raises(ToolError, match="consume_stock"):
        asyncio.run(server.call_tool("consume_stock", {"lot_id": 4, "quantity": 1}))
    assert not transport.calls, "a disabled write reached the API"


# ---------------------------------------------------------------------------
# Query shaping
# ---------------------------------------------------------------------------


def test_optional_query_parameters_are_omitted_rather_than_nulled() -> None:
    """A `root_id=None` on the wire would override the API's own default."""
    _, transport = call(
        "browse_locations",
        {},
        responses={("get", "/api/locations/tree"): {"nodes": []}},
    )
    assert transport.last["query"] == {"include_retired": False}


def test_requirement_lines_are_wrapped_as_the_api_expects() -> None:
    _, transport = call(
        "suggest_parts_for_requirements",
        {"lines": ["3x 10k 1% 0603", "100nF 50V X7R 0603"]},
        responses={("post", "/api/requirements/suggest"): {"lines": []}},
    )
    assert transport.last["body"]["lines"] == [
        {"text": "3x 10k 1% 0603"},
        {"text": "100nF 50V X7R 0603"},
    ]


def test_a_pick_lists_gaps_are_never_dropped_in_shaping() -> None:
    """The tool tells a model to report gaps. Shaping must not lose them."""
    payload, _ = call(
        "get_build_pick_list",
        {"build_id": 3},
        responses={
            ("get", "/api/builds/3/pick-list"): {
                "build_id": 3,
                "is_complete": False,
                "qty_milli": 5000,
                "stops": [
                    {
                        "location_id": 2,
                        "label_path": "Shop / Cab A / Bin 3",
                        "id_path": "1/2",
                        "short_id": None,
                        "qty_milli": 5000,
                        "takes": [],
                    }
                ],
                "gaps": [
                    {
                        "bom_line_id": 8,
                        "line_no": 8,
                        "kind": "short",
                        "part_id": 7,
                        "needed_milli": 4000,
                        "pickable_milli": 1000,
                        "shortfall_milli": 3000,
                    }
                ],
            }
        },
    )
    assert payload["is_complete"] is False
    assert payload["gaps"][0]["shortfall"] == 3.0
    assert payload["stops"][0]["qty"] == 5.0
