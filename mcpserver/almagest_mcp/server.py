"""Wiring: build the server, register the tools, speak stdio.

Deliberately thin. Everything interesting is in `tools.py` (what the tools do),
`coverage.py` (what is and is not exposed, and why) and `api.py` (how a call
reaches the API). This file only decides which of those get registered, and that
decision is one setting.

Transport is stdio, which is what a local MCP client launches as a subprocess and
what `.mcp.json` at the repo root configures. `MCPServer` also speaks
streamable-http, and this server would work over it unchanged — but it holds no
state, no session and no credentials, so running one as a shared network service
would add a hop and an access-control question for no gain over each client
launching its own.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from almagest_mcp import __version__
from almagest_mcp.api import ApiClient, HttpTransport, Transport
from almagest_mcp.config import McpSettings, get_settings
from almagest_mcp.tools import register_read_tools, register_write_tools

#: Shown to the model when it connects, before it has called anything. Kept to
#: what changes an answer: the three-tier stock model (because "how many do I
#: have" has no answer at the part level), and the two ways this system can say
#: "no" that a model would otherwise report as "you don't have any".
INSTRUCTIONS = """\
Almagest is a self-hosted electronic-component inventory: what parts exist, where \
they physically are, and how many remain.

Three things shape every answer here:

* **Quantity lives on a lot, not on a part.** A part is a definition; a lot is one \
physical package of it at one location. A reel and a cut-tape strip of the same \
MPN in the same bin are two lots. `get_part` lists them, and a movement names a \
lot.
* **Parametric search is a deterministic SQL filter over recorded parameters.** An \
empty result means "nothing recorded as matching", which is not the same as \
"nothing in the room" — a part whose parameters were never filled in cannot match. \
Say which one you mean. Call `list_filterable_fields` before filtering an \
unfamiliar category rather than guessing field names.
* **Suggestions are proposals.** `suggest_parts_for_requirements` and \
`get_bom_suggestions` rank candidates; they do not choose. A substitute with the \
wrong voltage rating is a field failure, so confirm against `get_part` before \
telling anyone to use one.

Physical workflows — provisioning a tag, printing labels, intake at the bench, \
staging a build — are deliberately not here. They need a person at the container.\
"""


def build_server(
    settings: McpSettings | None = None,
    transport: Transport | None = None,
) -> MCPServer[Any]:
    """Assemble the server.

    Both arguments are injectable so the tests can build the real server against a
    scripted transport — the registration logic, and specifically the write gate,
    is worth testing rather than trusting.
    """
    settings = settings or get_settings()
    client = ApiClient(transport or HttpTransport(settings.api_base_url, settings.timeout_s))

    server: MCPServer[Any] = MCPServer(
        name="almagest",
        title="Almagest inventory",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    register_read_tools(server, client)
    if settings.allow_writes:
        register_write_tools(server, client, settings)
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
