"""The guard that makes future work keep this server current.

This is the most valuable file in the package. Everything else can be re-derived
by reading the code; this is the only thing standing between "a curated MCP server"
and "a stale sixth of an API nobody remembers to extend".

The contract it enforces:

1. Every operation in `openapi.json` has exactly one disposition in `coverage.py`.
2. Every operation in `coverage.py` still exists in `openapi.json`.
3. Every `Exposed` operation has a route in `routes.py` and a tool actually
   registered on the server.
4. Every registered tool is claimed by the manifest — no tool exists that the
   manifest does not know about.
5. The write gate is honest: the write tools appear only when writes are on.
6. `README.md`'s three counts — operations, tools, refusals — still match the
   manifest, because the curation argument is made *with* those numbers.

Failure messages name the operations and say what to do, because the reader is
usually somebody — or something — who just added a route and has no idea this
package exists.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from almagest_mcp.config import McpSettings
from almagest_mcp.coverage import COVERAGE, WRITE_TOOLS, Excluded, Exposed, exposed_tools
from almagest_mcp.routes import ROUTES
from almagest_mcp.server import build_server


def registered_tool_names(*, allow_writes: bool) -> frozenset[str]:
    """The tool list the protocol would actually advertise.

    Goes through `list_tools()` rather than reading a registry, and drives it with
    `asyncio.run` rather than an async-pytest plugin — one fewer dependency, and
    these tests have no concurrency to model.
    """
    settings = McpSettings(ALMAGEST_MCP_ALLOW_WRITES=allow_writes)  # type: ignore[call-arg]
    server = build_server(settings=settings)
    return frozenset(tool.name for tool in asyncio.run(server.list_tools()))


# ---------------------------------------------------------------------------
# 1 & 2 — the manifest covers the API, exactly
# ---------------------------------------------------------------------------


def test_every_api_operation_has_a_disposition(
    operations: dict[str, tuple[str, str, dict[str, Any]]],
) -> None:
    """A backend route without a decision here fails the build. That is the point.

    If this is red, you added or renamed a route. Add a line to
    `almagest_mcp/coverage.py`:

        "your_operation_id": Exposed("your_tool_name"),       # and a tool + route
        "your_operation_id": Excluded(Reason.X, "why not"),   # a fine answer

    `Excluded` is the right answer for most routes — see the reasons in that file.
    Do not delete this test to get green.
    """
    missing = sorted(set(operations) - set(COVERAGE))
    assert not missing, (
        "these API operations have no entry in almagest_mcp/coverage.py:\n  "
        + "\n  ".join(
            f"{name}  ({operations[name][0].upper()} {operations[name][1]})" for name in missing
        )
        + "\n\nAdd each one as Exposed(tool) or Excluded(Reason.X, 'why'). "
        "Read the coverage.py docstring first."
    )


def test_the_manifest_names_no_operation_that_no_longer_exists(
    operations: dict[str, tuple[str, str, dict[str, Any]]],
) -> None:
    """The other direction: a deleted or renamed handler leaves a dead entry.

    A dead `Exposed` entry is a tool that 404s; a dead `Excluded` entry is a
    decision about nothing, which is worse than it sounds because it makes the
    manifest look more complete than it is.
    """
    stale = sorted(set(COVERAGE) - set(operations))
    assert not stale, (
        "almagest_mcp/coverage.py names operations that are not in openapi.json:\n  "
        + "\n  ".join(stale)
        + "\n\nEither the handler was renamed (rename the key — the operation id is "
        "the handler's function name) or it was deleted (delete the entry, and any "
        "tool and route that used it). If openapi.json is stale, run `make openapi`."
    )


# ---------------------------------------------------------------------------
# 3 & 4 — exposed means genuinely reachable
# ---------------------------------------------------------------------------


def test_every_exposed_operation_has_a_route() -> None:
    unrouted = sorted(
        operation_id
        for operation_id, disposition in COVERAGE.items()
        if isinstance(disposition, Exposed) and operation_id not in ROUTES
    )
    assert not unrouted, (
        f"marked Exposed but absent from routes.py, so no tool can call them: {unrouted}"
    )


def test_every_route_is_claimed_by_the_manifest() -> None:
    """A route nobody exposes is dead weight the contract test still validates."""
    unclaimed = sorted(
        operation_id
        for operation_id in ROUTES
        if not isinstance(COVERAGE.get(operation_id), Exposed)
    )
    assert not unclaimed, (
        f"in routes.py but not Exposed in coverage.py: {unclaimed}. Either expose "
        "them or remove the routes."
    )


def test_every_exposed_tool_is_actually_registered() -> None:
    """The manifest may not claim a tool that does not exist.

    Catches the likely half-finished edit: coverage updated, `tools.py` not — which
    would otherwise pass every other test here and fail only when a model called
    the tool.
    """
    registered = registered_tool_names(allow_writes=True)
    claimed = exposed_tools()
    assert not (claimed - registered), (
        f"coverage.py claims tools that tools.py does not register: {sorted(claimed - registered)}"
    )


def test_every_registered_tool_is_claimed_by_the_manifest() -> None:
    """And the reverse: no tool may exist without a manifest entry pointing at it.

    This is what stops the manifest being bypassed. Adding a tool without touching
    `coverage.py` fails here, so the file cannot fall out of date by addition
    either.
    """
    registered = registered_tool_names(allow_writes=True)
    claimed = exposed_tools()
    assert not (registered - claimed), (
        f"tools.py registers tools that no coverage.py entry claims: "
        f"{sorted(registered - claimed)}. Mark the operation each one calls as "
        "Exposed('tool_name')."
    )


# ---------------------------------------------------------------------------
# 5 — the write gate
# ---------------------------------------------------------------------------


def test_writes_are_absent_unless_enabled() -> None:
    """Absent, not present-and-refusing. See `McpSettings.allow_writes`."""
    read_only = registered_tool_names(allow_writes=False)
    assert not (read_only & WRITE_TOOLS), (
        f"write tools registered with ALMAGEST_MCP_ALLOW_WRITES off: "
        f"{sorted(read_only & WRITE_TOOLS)}"
    )
    assert read_only, "the read surface must exist regardless"


def test_writes_are_all_present_when_enabled() -> None:
    with_writes = registered_tool_names(allow_writes=True)
    assert with_writes >= WRITE_TOOLS, (
        f"missing write tools with writes on: {sorted(WRITE_TOOLS - with_writes)}"
    )


def test_the_read_surface_is_annotated_read_only() -> None:
    """A model and a permission prompt both read `read_only_hint`.

    A read tool that forgets the annotation gets confirmation-prompted like a
    write, which trains people to click through the prompts that matter.
    """
    settings = McpSettings(ALMAGEST_MCP_ALLOW_WRITES=True)  # type: ignore[call-arg]
    server = build_server(settings=settings)
    for tool in asyncio.run(server.list_tools()):
        annotations = tool.annotations
        assert annotations is not None, f"{tool.name} has no annotations"
        expected = tool.name not in WRITE_TOOLS
        assert annotations.read_only_hint is expected, (
            f"{tool.name}: read_only_hint={annotations.read_only_hint}, expected {expected}"
        )


# ---------------------------------------------------------------------------
# The notes are load-bearing, so they are checked
# ---------------------------------------------------------------------------


def test_every_exclusion_explains_itself() -> None:
    """An `Excluded` with an empty or one-word note is a decision nobody can revisit.

    The reason enum says which *category* of thing it is; the note has to say what
    is true about this route in particular, because the next person's question is
    always "would exposing this actually be wrong?"
    """
    thin = sorted(
        operation_id
        for operation_id, disposition in COVERAGE.items()
        if isinstance(disposition, Excluded) and len(disposition.note.split()) < 6
    )
    assert not thin, f"these exclusions need a real explanation: {thin}"


def test_no_two_operations_are_exposed_as_the_same_tool_by_accident() -> None:
    """Two operations may share a tool only if the tool really calls both.

    Nothing does today. If that changes, delete this test and say why in the
    manifest — but a duplicate is far more likely to be a copy-paste in
    `Exposed("...")`, which would silently leave one operation unreachable while
    reading as covered.
    """
    tools = [
        disposition.tool for disposition in COVERAGE.values() if isinstance(disposition, Exposed)
    ]
    duplicates = sorted({tool for tool in tools if tools.count(tool) > 1})
    assert not duplicates, f"more than one operation Exposed as the same tool: {duplicates}"


# ---------------------------------------------------------------------------
# So do the counts in the prose
# ---------------------------------------------------------------------------


def test_the_readme_counts_match_the_manifest() -> None:
    """`README.md` states three numbers, and a stale one misleads on its own.

    "26 tools, and 116 deliberate refusals" out of 142 operations is the whole
    argument for curation, so a reader who finds those numbers wrong has no reason
    to trust the paragraph they are in. Nothing else notices: the manifest test
    above keeps `coverage.py` honest against the schema, and the prose is outside
    both.

    Checks **every** occurrence rather than asserting the right string appears
    somewhere. Each number is written more than once — the table, the heading, the
    paragraph making the argument — and a first draft of this test that only looked
    for one match passed happily on a README where the heading said 26 and the
    table said 25. Half-updated prose is the failure mode that actually happens.
    """
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()

    expected = {
        "operations": len(COVERAGE),
        "tools": len(exposed_tools()),
        "deliberate refusals": sum(
            1 for disposition in COVERAGE.values() if isinstance(disposition, Excluded)
        ),
    }

    for noun, count in expected.items():
        written = [int(found) for found in re.findall(rf"(\d+) {noun}\b", readme)]
        assert written, (
            f"mcpserver/README.md never says how many {noun} there are. "
            f"It should say {count}: that number is the curation argument."
        )
        wrong = sorted({found for found in written if found != count})
        assert not wrong, (
            f"mcpserver/README.md says {wrong} {noun}; the manifest holds {count}. "
            f"Every mention has to move together — update the prose, and do not "
            f"delete this test, or the argument gets made with last season's numbers."
        )
