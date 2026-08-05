"""What chat is allowed to look up, and why these run in-process.

## The re-entry question, answered rather than dodged

ADR 0018 moves the agent loop out of the API because *an agent whose tools call
back into a single-replica SQLite writer, from inside that writer's own process,
is a self-deadlock*. The hazard is **HTTP re-entry**: the request handler is
occupied, and its tool call needs another handler and another connection from the
same bounded pool.

These tools do not make an HTTP call. They run against the **session the request
already holds**, as ordinary reads — the same thing any other line of the handler
does. There is no second request, no second connection, and therefore no
deadlock. Re-entry was the problem; the loop never was.

That is a deliberate divergence from ADR 0018's "reuse the `mcpserver` tool
surface", and the reason is the same sentence: `mcpserver` is an **HTTP client of
this API**, so calling it from inside a request handler is precisely the re-entry
the ADR forbids. Reusing it would have to wait for the agent loop to move out of
process. Until then these are a small, deliberately duplicated read surface, and
the duplication is worth naming: **if a tool here and its `mcpserver` twin ever
disagree, `mcpserver` is right** — it is the curated one with the coverage test
behind it.

## Read-only, and that is not a placeholder

Every tool below is a query. Nothing here writes, and ADR 0018's split — chat may
do *reversible authoring*, never *irreversible record-keeping* — is not yet
implemented at all, so the honest position today is that chat can look and cannot
touch. Creating containers is the first authoring tool to add, and it needs the
confirm-before-run UI the ADR describes, not just a function.

## Whole units at the boundary, always

Quantities are stored as `*_milli` integers and are handed to the model as whole
units, exactly as `mcpserver` does. A model shown `620000` will say "620,000 in
stock", and it will be sincerely, uncorrectably wrong.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.storage import Location
from app.services.search import query_builder

#: Most rows any one tool returns. The model pays for every one of them in
#: context, and a hundred-row answer is one it will summarise badly rather than
#: read. A truncated result says so, so the model can narrow rather than conclude
#: the shelf is empty.
MAX_ROWS = 15


def _units(milli: int | None) -> float:
    """`*_milli` to whole units. See the module docstring — this is not cosmetic."""
    return round((milli or 0) / 1000, 3)


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_parts",
            "description": (
                "Search the inventory by free text — a part number, a description, or "
                "words like 'ceramic capacitor'. Returns what is recorded, with "
                "quantities in whole units and the locations holding them. An empty "
                "result means nothing is RECORDED as matching, which is not the same "
                "as nothing being in the room."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free text to match."},
                    "in_stock_only": {
                        "type": "boolean",
                        "description": "Only parts with quantity above zero.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_locations",
            "description": (
                "List storage locations by name or path fragment, so you can say where "
                "something lives or what a drawer is called."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": [],
            },
        },
    },
]


def _search_parts(session: Session, args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("query") or "").strip()
    if not text:
        return {"error": "query is required"}
    parts = query_builder.execute(
        session,
        query_builder.SearchQuery(
            text=text,
            in_stock_only=bool(args.get("in_stock_only", False)),
            limit=MAX_ROWS + 1,
        ),
    )
    truncated = len(parts) > MAX_ROWS
    rows = [
        {
            "part_id": part.id,
            "name": part.name,
            "mpn": part.mpn,
            # Whole units, and summed across lots because "how many do I have" is a
            # question about the part, while the lot is where it physically is.
            "qty": _units(sum(lot.qty_milli_cached or 0 for lot in getattr(part, "lots", []))),
            "locations": sorted(
                {
                    lot.location.label_path or lot.location.name
                    for lot in getattr(part, "lots", [])
                    if lot.location is not None
                }
            ),
        }
        for part in parts[:MAX_ROWS]
    ]
    out: dict[str, Any] = {"results": rows, "count": len(rows)}
    if truncated:
        # Said out loud: a silently cut list reads as "that is all of them", and the
        # model will report it as such.
        out["truncated"] = f"more than {MAX_ROWS} matched; narrow the query"
    return out


def _list_locations(session: Session, args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("query") or "").strip()
    statement = select(Location).where(Location.retired_at.is_(None))
    if text:
        statement = statement.where(Location.name.icontains(text))
    found = list(session.execute(statement.limit(MAX_ROWS)).scalars().all())
    return {
        "results": [{"location_id": row.id, "path": row.label_path or row.name} for row in found],
        "count": len(found),
    }


_DISPATCH = {"search_parts": _search_parts, "list_locations": _list_locations}


def call(session: Session, name: str, arguments: str | dict[str, Any]) -> str:
    """Run one tool and return its result as JSON text for the model.

    A refused or unknown call comes back as `{"error": ...}` rather than raising:
    the model asked for something it should not have, and telling it so lets it
    recover in the next turn. Raising would end the conversation over a mistake the
    model can correct.
    """
    args: dict[str, Any]
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": f"arguments for {name} were not valid JSON"})
    else:
        args = dict(arguments)

    handler = _DISPATCH.get(name)
    if handler is None:
        return json.dumps({"error": f"no such tool: {name}"})
    try:
        return json.dumps(handler(session, args), default=str)
    except Exception as error:  # a query's exception types are not enumerable
        # Reported to the model, not raised: a malformed filter is something it can
        # try differently, and a 500 in the middle of a chat turn is not.
        return json.dumps({"error": f"{type(error).__name__}: {error}"})
