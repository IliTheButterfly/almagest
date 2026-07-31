"""Shared fixtures, and the hook that keeps the live test out of CI.

Mirrors `backend/tests/conftest.py` and `deviceagent/tests/conftest.py`:
`pyproject.toml` registers `live` as "skipped unless `-m live` is passed", and
`make mcp-test` runs a bare pytest, so without the hook below that sentence would
be a comment rather than a behaviour. It matters more here than for the agent's
hardware tests, because a developer machine often *does* have `make run` going —
so the live test would pass locally and fail in CI, which is the worst possible
arrangement.

**Skipped, not deselected.** A deselected test is invisible; the point of the live
test is to be a visible reminder that a contract exists which nothing in CI
exercises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from almagest_mcp.api import ApiClient, Transport

#: `make openapi` regenerates this and CI fails if it is stale, so the schema read
#: here is always the one the running API serves. Same file the frontend's client
#: and the deviceagent's contract test read.
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "openapi.json"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "live" in str(config.getoption("markexpr", default="") or ""):
        return
    skip_live = pytest.mark.skip(
        reason="needs a running Almagest API: run with `-m live` (make mcp-test-live)"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def operations(schema: dict[str, Any]) -> dict[str, tuple[str, str, dict[str, Any]]]:
    """operation id → (method, path, operation object).

    Keyed by operation id because that is what `routes.py` and `coverage.py` are
    keyed by, and because `backend/app/main.py` makes it the handler's function
    name — so a rename is caught rather than silently re-pointing at a path that
    still exists.
    """
    return {
        operation["operationId"]: (method, path, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
        if "operationId" in operation
    }


class ScriptedTransport:
    """A `Transport` that records requests and answers from a canned table.

    The tools' job is to translate — arguments in, request out, response shaped —
    and that is exactly what a real server makes tedious to assert. What a real
    server *would* additionally check is that the routes and fields exist, and
    `test_api_contract.py` gets that from `openapi.json` without one.
    """

    def __init__(self, responses: dict[tuple[str, str], Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        return self.responses.get((method, path))

    @property
    def last(self) -> dict[str, Any]:
        assert self.calls, "no request was made"
        return self.calls[-1]


@pytest.fixture
def transport() -> ScriptedTransport:
    return ScriptedTransport()


@pytest.fixture
def client(transport: ScriptedTransport) -> ApiClient:
    return ApiClient(transport)


def _assert_is_transport(candidate: Transport) -> None:
    """Keeps `Transport` imported and structurally satisfied by the fake.

    Without this, a signature change on the Protocol would only be caught by mypy
    on `almagest_mcp/`, which does not look at `tests/`.
    """


_assert_is_transport(ScriptedTransport())
