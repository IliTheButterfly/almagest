"""Every API route this server calls, in one table.

Tools never write a URL. They name an operation — `client.call("read_part", ...)`
— and the method and path come from here, so there is exactly one place where
this package's idea of the API lives.

That indirection is the whole point. `tests/test_api_contract.py` reads the
committed `openapi.json` and asserts that every entry below is a real operation
with that exact method and path, matched **by `operationId`**. FastAPI's
`generate_unique_id_function` makes the operation id the handler's function name
(see `backend/app/main.py`), so:

* a route moved to a different path fails here,
* a handler renamed fails here,
* a handler deleted fails here,

all in `make check`, naming the operation. A hand-written URL scattered across
twenty tool bodies would have failed at runtime, in front of a user, as a 404
that reads like "no such part".

`CLAUDE.md` says API clients are generated, never hand-written. This is
hand-written for the same reason `deviceagent/agent/api.py` is, and with the same
compensating control: twenty-five operations, no auth flow, no streaming, and the
contract test above. If this table outgrows a screen, generate the client.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class Route:
    """One API operation.

    `path` is a `str.format` template — `{part_id}` — filled from a tool's
    validated arguments. Templates are the *OpenAPI* path exactly, placeholders
    included, so the contract test can compare strings rather than patterns.
    """

    method: str
    path: str


def _get(path: str) -> Route:
    return Route("get", path)


def _post(path: str) -> Route:
    return Route("post", path)


#: operation id → route. Keys are `openapi.json` operation ids; nothing else is a
#: valid key, and `coverage.py` may only mark an operation `exposed` if it appears
#: here.
ROUTES: Final[Mapping[str, Route]] = MappingProxyType(
    {
        # -- the catalogue ---------------------------------------------------
        "search_parts": _post("/api/search/parts"),
        "read_part": _get("/api/parts/{part_id}"),
        "search_datasheets": _get("/api/search/datasheets"),
        "suggest_parts": _post("/api/requirements/suggest"),
        # Both of these exist so a model does not have to *guess* the vocabulary
        # `search_parts` takes. Without them it invents template names ("cap",
        # "capacitance_uF") and category slugs, gets an empty result set, and
        # reports "you don't have any" — which is the single worst failure this
        # server can produce, because it is indistinguishable from a true answer.
        "parameter_facets": _post("/api/parameter-templates"),
        "list_part_categories": _get("/api/part-categories"),
        "list_part_kinds": _get("/api/part-kinds"),
        "list_part_parameters": _get("/api/parts/{part_id}/parameters"),
        # -- identity and place ----------------------------------------------
        "resolve_short_id": _get("/api/resolve/{short_id}"),
        "read_location": _get("/api/locations/{location_id}"),
        "read_location_tree": _get("/api/locations/tree"),
        "read_lot": _get("/api/stock/lots/{lot_id}"),
        "read_lot_history": _get("/api/stock/lots/{lot_id}/history"),
        # -- projects and builds ---------------------------------------------
        "list_projects": _get("/api/projects"),
        "read_project": _get("/api/projects/{project_id}"),
        "list_bom_lines": _get("/api/projects/{project_id}/bom"),
        "read_bom_suggestions": _get("/api/projects/{project_id}/bom/suggestions"),
        "read_shortages": _get("/api/builds/{build_id}/shortages"),
        "read_pick_list": _get("/api/builds/{build_id}/pick-list"),
        # -- movements: the write surface, off unless ALMAGEST_MCP_ALLOW_WRITES
        "consume_stock": _post("/api/stock/lots/{lot_id}/consume"),
        "return_stock": _post("/api/stock/lots/{lot_id}/return"),
        "move_stock": _post("/api/stock/lots/{lot_id}/move"),
        "recount_stock": _post("/api/stock/lots/{lot_id}/recount"),
        "undo_movement": _post("/api/stock/undo"),
        # -- diagnostics -----------------------------------------------------
        "health": _get("/api/system/health"),
        "read_caches": _get("/api/system/caches"),
    }
)
