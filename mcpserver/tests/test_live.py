"""One end-to-end read against a real API. Skipped unless `-m live` is passed.

Everything else here runs against a scripted transport, which proves the
translation and proves the contract against `openapi.json` — but not that
`HttpTransport` actually speaks to a live FastAPI: a wrong `Content-Type`, a
trailing-slash redirect, a query encoded as a stringified list. Those only show up
against a server.

Kept visible in the summary as a skip rather than deleted, for the same reason the
`deviceagent`'s hardware tests are: an unexercised contract you can see beats one
you cannot.

    cd mcpserver && uv run pytest -m live      # needs `make run` in another shell
"""

from __future__ import annotations

import asyncio

import pytest

from almagest_mcp.api import ApiClient, ApiUnavailable, HttpTransport
from almagest_mcp.config import get_settings

pytestmark = pytest.mark.live


def test_the_transport_reaches_a_real_api() -> None:
    settings = get_settings()
    client = ApiClient(HttpTransport(settings.api_base_url, settings.timeout_s))
    try:
        payload = asyncio.run(client.call("health"))
    except ApiUnavailable as exc:  # pragma: no cover - only without a server
        pytest.fail(f"no API at {settings.api_base_url}: {exc}. Run `make run` first.")
    assert payload["status"]
    assert payload["schema_revision"], "a migrated database reports its revision"


def test_a_query_string_search_round_trips() -> None:
    """A POST with a JSON body and a real response, which is the shape most tools use."""
    settings = get_settings()
    client = ApiClient(HttpTransport(settings.api_base_url, settings.timeout_s))
    payload = asyncio.run(client.call("search_parts", body={"text": "", "limit": 1}))
    assert "total" in payload
    assert isinstance(payload["results"], list)
