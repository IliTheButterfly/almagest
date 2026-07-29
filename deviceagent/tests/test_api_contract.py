"""`agent.api` against the committed `openapi.json`. The client is hand-written.

`CLAUDE.md`: "API clients are **generated** from FastAPI's OpenAPI schema, never
hand-written — that is what makes the cross-repo splits safe." `agent/api.py` is
hand-written, so this file is the compensating control. It reads the schema the
frontend's client is generated from and asserts that every path the agent calls
exists, with the request fields it sends and the response fields it reads.

That covers the failure the rule exists to prevent: a route or field renamed in the
backend becomes a red `make check` here rather than a station that says
`malformed_response` at a bench. It does **not** make the client generated — if
this ever grows past a handful of endpoints, generate it.

`make openapi` regenerates the file and CI fails if it is stale, so the schema read
here is always the one the running API serves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.api import (
    LOCATION_PATH,
    QTY_MILLI_MAX,
    RESOLVE_PATH,
    ActionKind,
    route_for,
)

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def deref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if not isinstance(ref, str):
        return node
    resolved: Any = schema
    for part in ref.lstrip("#/").split("/"):
        resolved = resolved[part]
    return deref(schema, resolved)


def properties(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """The property map of a schema node, following `$ref` and `anyOf`.

    `anyOf` is how Pydantic renders an optional field (`X | None`), so a nullable
    response object like `TagResolveResponse.location` is a two-branch union whose
    real shape is in the first branch.
    """
    node = deref(schema, node)
    found = node.get("properties")
    if isinstance(found, dict):
        return found
    for branch in (*node.get("anyOf", []), *node.get("allOf", [])):
        nested = properties(schema, branch)
        if nested:
            return nested
    return {}


def operation(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    paths = schema["paths"]
    assert path in paths, f"{path} is not in openapi.json; the agent calls it"
    assert method in paths[path], f"{path} has no {method.upper()}"
    result: dict[str, Any] = paths[path][method]
    return result


def request_properties(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    body = operation(schema, path, method)["requestBody"]
    return properties(schema, body["content"]["application/json"]["schema"])


def response_properties(schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    ok = operation(schema, path, method)["responses"]["200"]
    return properties(schema, ok["content"]["application/json"]["schema"])


# ---------------------------------------------------------------------------
# Resolve — the identify half
# ---------------------------------------------------------------------------


def test_the_resolve_route_takes_both_carriers(schema: dict[str, Any]) -> None:
    """Both, always: only the server, seeing both, can report a disagreement."""
    sent = request_properties(schema, RESOLVE_PATH, "post")
    assert {"tag_uid", "ndef_url"} <= set(sent)


def test_the_resolve_route_reports_what_the_station_renders(schema: dict[str, Any]) -> None:
    read = response_properties(schema, RESOLVE_PATH, "post")
    assert {"status", "matched_by", "location", "disagreement"} <= set(read)
    location = properties(schema, read["location"])
    assert "location_id" in location


# ---------------------------------------------------------------------------
# The container read — PLAN.md's READY screen
# ---------------------------------------------------------------------------


def test_the_location_route_carries_name_path_short_id_and_lots(schema: dict[str, Any]) -> None:
    """PLAN.md's `READY`: name, path, short_id, ledger balance. Four of the five
    fields come from here; the fifth was the weight-derived count, and ADR 0003
    deferred the scale that produced it."""
    read = response_properties(schema, LOCATION_PATH.format(location_id="{location_id}"), "get")
    assert {"id", "name", "label_path", "short_id", "lots"} <= set(read)

    lot = properties(schema, deref(schema, read["lots"])["items"])
    assert {
        "id",
        "part_id",
        "qty_milli",
        "qty_reserved_milli",
        "status",
        "batch_code",
    } <= set(lot)


# ---------------------------------------------------------------------------
# The three movements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(ActionKind))
def test_every_action_names_a_route_that_exists(schema: dict[str, Any], kind: ActionKind) -> None:
    """Three actions because three routes exist. A renamed route fails here."""
    route = route_for(kind)
    path = route.path.format(lot_id="{lot_id}")
    sent = request_properties(schema, path, "post")
    assert route.qty_field in sent, f"{path} has no {route.qty_field}"
    # Without these three a station movement would be anonymous and
    # non-idempotent: `client_op_id` is what makes a retry replay, `device_id`
    # says which bench did it, `source` records that a tag read captured it.
    assert {"client_op_id", "device_id", "source"} <= set(sent)


@pytest.mark.parametrize("kind", list(ActionKind))
def test_every_movement_response_carries_the_rows_and_the_new_balance(
    schema: dict[str, Any], kind: ActionKind
) -> None:
    path = route_for(kind).path.format(lot_id="{lot_id}")
    read = response_properties(schema, path, "post")
    assert {"seqs", "lot", "replayed"} <= set(read)


def test_the_agents_quantity_ceiling_matches_the_apis(schema: dict[str, Any]) -> None:
    """`QTY_MILLI_MAX` is restated in `agent.api` so a nonsense quantity is refused
    at the bench instead of costing a round trip to be told 422. Restated, so it has
    to be checked: a widened bound backend-side must not silently leave the agent
    refusing things the API would accept."""
    sent = request_properties(
        schema, route_for(ActionKind.TAKE).path.format(lot_id="{lot_id}"), "post"
    )
    assert sent["qty_milli"]["maximum"] == QTY_MILLI_MAX


def test_the_ledger_source_the_station_reports_is_a_real_one(schema: dict[str, Any]) -> None:
    """`scan`: the container identified itself by tag. `manual` would erase that,
    and `scale` would claim a measurement ADR 0003 says does not exist."""
    from agent.api import DEFAULT_LEDGER_SOURCE

    sent = request_properties(
        schema, route_for(ActionKind.TAKE).path.format(lot_id="{lot_id}"), "post"
    )
    source = deref(schema, sent["source"])
    assert DEFAULT_LEDGER_SOURCE in source["enum"]
