"""The HTTP client, and the two failure modes worth telling a model apart.

**Why `urllib` rather than an HTTP library.** Same answer as
`deviceagent/agent/api.py`: no streaming, no auth flow, no retries of its own, and
a dependency here is a dependency in every environment an agent runs in. The
blocking call goes through `asyncio.to_thread` because MCP tool handlers are
coroutines on the same loop that is serving stdio — a slow BOM suggestion must not
stall the protocol.

**Why the errors are two classes and not one.** A model decides what to do next
from the error text, and the two cases have opposite right answers:

* `ApiUnavailable` — nothing answered. Nothing happened, so retrying is safe;
  for a write, retrying with the *same* `client_op_id` is safe specifically
  because the server deduplicates on it.
* `ApiError` — something answered and said no. Retrying is pointless. `reason` is
  the machine-readable code the API's own error bodies carry, surfaced verbatim so
  the model reports what the server said rather than a guess.

Collapsing the two teaches a model to retry refusals, which for a write means
hammering a route that has already decided the answer is no.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final, Protocol

from almagest_mcp.routes import ROUTES

#: Sent so a request from this server is identifiable in the API's access log
#: without reading the body.
USER_AGENT: Final = "almagest-mcp"


class ApiUnavailable(RuntimeError):
    """Nothing answered — DNS, connection, timeout, or a 5xx.

    A 5xx belongs here rather than in `ApiError`: the server did not *decide*
    anything, it fell over, and the useful advice is the same as for a dropped
    connection.
    """


class ApiError(RuntimeError):
    """The API answered and refused.

    `reason` is the API's own code (`{"detail": {"reason": ..., "message": ...}}`)
    where it carries one, `"http_<status>"` where it does not. Never invented
    here: a vocabulary this package made up would drift from the one the PWA shows
    a human for the same refusal.
    """

    def __init__(self, status: int, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.status = status
        self.reason = reason
        self.message = message


class Transport(Protocol):
    """What `ApiClient` needs from the world.

    A Protocol so the tool tests can drive a scripted transport instead of a live
    API. The tests that matter here are "does the tool send the right request and
    shape the answer usefully", and those are exactly the tests that a real server
    makes slow and flaky to write.
    """

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any: ...


class HttpTransport:
    """`urllib` against a real API."""

    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        return await asyncio.to_thread(self._request_blocking, method, path, query, body)

    def _request_blocking(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
    ) -> Any:
        url = self._base_url + path
        if query:
            # `doseq` so a repeated query parameter (`status=planned&status=active`)
            # renders as repeats rather than as a stringified Python list, which is
            # what FastAPI parses a `list[...]` query parameter from.
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"

        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url=url, method=method.upper(), data=data)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _from_http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiUnavailable(f"{self._base_url} did not answer: {exc}") from exc

        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # Answered, but not with the contract. Not `ApiError`: there is no
            # refusal to report, and something is wrong with the deployment (a
            # proxy's error page, most likely) rather than with the request.
            raise ApiUnavailable(f"{url} returned a non-JSON body") from exc


def _from_http_error(exc: urllib.error.HTTPError) -> Exception:
    if exc.code >= 500:
        return ApiUnavailable(f"the API returned HTTP {exc.code}")
    reason, message = _decode_error_body(exc)
    return ApiError(exc.code, reason, message)


def _decode_error_body(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Pull `reason`/`message` out of whichever error shape arrived.

    Three shapes exist and all three are the API's, so all three are handled here
    rather than in each tool: the project's own `{"detail": {"reason", "message"}}`,
    FastAPI's `{"detail": "some string"}` from a bare `HTTPException`, and its
    422 `{"detail": [{"loc", "msg", ...}]}` from request validation.
    """
    fallback = f"http_{exc.code}"
    try:
        payload = json.loads(exc.read() or b"")
    except (json.JSONDecodeError, OSError):
        return fallback, exc.reason or "the API refused the request"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        reason = detail.get("reason")
        message = detail.get("message")
        return (
            reason if isinstance(reason, str) else fallback,
            message if isinstance(message, str) else json.dumps(detail),
        )
    if isinstance(detail, str):
        return fallback, detail
    if isinstance(detail, list):
        # A 422 is a bug in *this* package's argument handling — the tool schema
        # should have caught it — so keep the field locations, which are what
        # names the offending argument.
        parts = [
            f"{'.'.join(str(bit) for bit in item.get('loc', []))}: {item.get('msg', '')}".strip(
                ": "
            )
            for item in detail
            if isinstance(item, dict)
        ]
        return "invalid_request", "; ".join(parts) or "the API rejected the request body"
    return fallback, exc.reason or "the API refused the request"


class ApiClient:
    """Calls operations by id. The only thing tools are given."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def call(
        self,
        operation_id: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        route = ROUTES[operation_id]
        path = route.path.format(**(path_params or {}))
        # Dropping `None`s here rather than at each call site is what lets a tool
        # pass every optional argument straight through and let the API's own
        # defaults apply. A `None` sent explicitly would override them.
        return await self._transport.request(
            route.method,
            path,
            query=_without_nones(query),
            body=_without_nones(body),
        )


def _without_nones(values: dict[str, Any] | None) -> dict[str, Any] | None:
    if values is None:
        return None
    kept = {key: value for key, value in values.items() if value is not None}
    return kept or None
