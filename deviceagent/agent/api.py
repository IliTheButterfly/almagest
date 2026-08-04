"""The agent's client for the Almagest API — its only way to change anything.

**The agent is not a second ledger writer.** Every commit made at the bench goes
through the same `POST /api/stock/lots/{id}/…` routes the PWA uses, so
`app/services/ledger.py` stays the sole writer, server-side idempotency applies
unchanged, and a station take is indistinguishable in the ledger from a take
entered by hand except for its `source` and `device_id`. Nothing here imports
`app.models` or opens a database: the agent runs on the Pi and the SQLite file is
in the cluster, and a second writer on a ReadWriteOnce volume is corruption.

**Why `urllib` rather than an HTTP library.** This is five requests with no
streaming, no auth flow and no retries of its own, and `pyproject.toml`'s policy
is that every dependency here is one more wheel to pin on a machine nobody wants
to debug at a bench. The blocking call runs in `asyncio.to_thread`, exactly as the
reader poll does and for the same reason: a slow API must not stall the event loop
that is serving the kiosk.

**Why a hand-written client is allowed here, given `CLAUDE.md` says API clients
are generated.** That rule is what makes the cross-repo splits safe, and the
compensating control for this one is `tests/test_api_contract.py`: it reads the
committed `openapi.json` and asserts that every path this module calls exists,
with the request fields it sends and the response fields it reads. A route
renamed in the backend fails there, in `make check`, rather than at a bench. If
this client ever grows past a handful of endpoints, generate it — do not keep
hand-writing.

Two failure modes, kept apart because the answer to them differs:

* `ApiUnavailable` — nothing answered. Retrying the *same* request with the
  *same* `client_op_id` is always safe and is what the session does.
* `ApiError` — something answered and said no. `reason` is the machine-readable
  code the API's own error bodies carry (`{"detail": {"reason", "message"}}`), so
  the station can surface it verbatim instead of inventing a vocabulary.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

#: `LedgerSource.SCAN`. A station movement was captured by *reading a tag*, which
#: is a scan; `MANUAL` would erase the fact that the container identified itself,
#: and `SCALE` would claim a measurement ADR 0003 says does not exist. The value
#: is a plain string rather than an import of `app.models.enums` so that the wire
#: contract of this module is visible in this module.
DEFAULT_LEDGER_SOURCE: Final = "scan"

#: Matches `app.api.limits.QTY_MILLI_MAX`, restated rather than imported because
#: it is a bound on what this client will *send*, and a request the API would
#: reject as 422 should never leave the Pi. `tests/test_api_contract.py` asserts
#: the two agree, so a widened bound backend-side is not silently missed here.
QTY_MILLI_MAX: Final = 10**12


class ActionKind(StrEnum):
    """What a station action does. Three, because three routes exist.

    The vocabulary is the API's, not the UI's: `take` is `consume`, `add` is
    `return`, `recount` sets the balance to what was physically counted. PLAN.md's
    fourth station action — "pour into the counting tray" — needs vision counting,
    which is a later phase, and intake (`receive`) is workflow 1's job because it
    needs a part and a destination the station has no way to know.

    A `StrEnum` off-database is safe: the no-`sa.Enum` rule exists because SQLite
    cannot alter a `CHECK` constraint, and nothing here touches a column.
    """

    TAKE = "take"
    ADD = "add"
    RECOUNT = "recount"


@dataclass(frozen=True, slots=True)
class Action:
    """One proposed movement: what, to which lot, how much.

    Lives here beside `ActionKind` rather than in `agent.session` because it is
    exactly the argument list of a commit — and because `agent.events` needs to
    render it, which would otherwise be an import cycle.

    Frozen, and compared by value: "is this the same action the user already
    confirmed?" is the question both the hold-off and the idempotency-key rule ask,
    and both would be wrong if two equal proposals were distinguishable.
    """

    kind: ActionKind
    lot_id: int
    qty_milli: int

    def as_data(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "lot_id": self.lot_id, "qty_milli": self.qty_milli}

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}:{self.lot_id}:{self.qty_milli}"


@dataclass(frozen=True, slots=True)
class _Route:
    path: str
    #: `recount` names its quantity differently because it means something
    #: different: a counted total, not an amount moved. Zero is legitimate for it
    #: and refused for the other two, which is why `minimum` lives here too.
    qty_field: str
    minimum: int


_ROUTES: Final[dict[ActionKind, _Route]] = {
    ActionKind.TAKE: _Route("/api/stock/lots/{lot_id}/consume", "qty_milli", 1),
    ActionKind.ADD: _Route("/api/stock/lots/{lot_id}/return", "qty_milli", 1),
    ActionKind.RECOUNT: _Route("/api/stock/lots/{lot_id}/recount", "counted_qty_milli", 0),
}

RESOLVE_PATH: Final = "/api/location-tags/resolve"
LOCATION_PATH: Final = "/api/locations/{location_id}"


def route_for(kind: ActionKind) -> _Route:
    """The one place an action becomes a URL. Exposed for the contract test."""
    return _ROUTES[kind]


class ApiError(Exception):
    """The API answered and refused, or answered something unreadable.

    `status` is `None` for a body this client could not parse — a version skew
    rather than an HTTP failure. It is folded in here rather than given a third
    exception type because the station's response is the same as for any refusal:
    say so, keep the pending action, and let the user retry or cancel.
    """

    def __init__(self, message: str, *, status: int | None, reason: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


class ApiUnavailable(Exception):
    """Nothing answered: the API is down, or the network is. Retry is safe."""


# ---------------------------------------------------------------------------
# Read models — the slices of the API's responses the station actually renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LotView:
    """One lot in the container, with its balance.

    `qty_milli` is `stock_lots.qty_milli_cached` as the API renders it — never a
    sum over the ledger, which is the query that stops being sub-second around
    200k rows and would be on every placement.

    Deliberately carries no part *name*. Resolving one would be an HTTP round trip
    per lot on the hot path, and the PWA already has a typed client and can render
    `part_id` however it likes. The agent is not a display layer.
    """

    lot_id: int
    part_id: int
    qty_milli: int
    qty_reserved_milli: int
    status: str
    batch_code: str | None

    def as_data(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "part_id": self.part_id,
            "qty_milli": self.qty_milli,
            "qty_reserved_milli": self.qty_reserved_milli,
            "status": self.status,
            "batch_code": self.batch_code,
        }


@dataclass(frozen=True, slots=True)
class ContainerView:
    """What `READY` shows: name, derived path, short id, and the balances.

    `label_path` is derived by the API on every read and never taken from the tag
    — a container that moves would make an encoded path a lie the moment the
    drawer changed cabinet.

    PLAN.md's `READY` also lists a weight-derived count. ADR 0003 deferred the
    load cell, so that number is simply absent, which is the contract the whole
    design already promised: no `weight.*` event arrives, so the affordance is
    never drawn. There is no flag for its absence and there must not be one.
    """

    location_id: int
    name: str
    label_path: str
    short_id: str | None
    lots: tuple[LotView, ...]

    @property
    def total_qty_milli(self) -> int:
        return sum(lot.qty_milli for lot in self.lots)

    def lot(self, lot_id: int) -> LotView | None:
        return next((lot for lot in self.lots if lot.lot_id == lot_id), None)

    def with_lot(self, updated: LotView) -> ContainerView:
        """Replace one lot's balance, keeping everything else.

        Used after a commit: the movement response is authoritative for the lot
        that moved, so patching it beats re-reading the whole container — one
        fewer round trip on the loop back to `ACTION`. Anything that changed
        *elsewhere* is what `station.refresh` is for.
        """
        return ContainerView(
            location_id=self.location_id,
            name=self.name,
            label_path=self.label_path,
            short_id=self.short_id,
            lots=tuple(updated if lot.lot_id == updated.lot_id else lot for lot in self.lots),
        )

    def as_data(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "name": self.name,
            "label_path": self.label_path,
            "short_id": self.short_id,
            "total_qty_milli": self.total_qty_milli,
            "lots": [lot.as_data() for lot in self.lots],
        }


@dataclass(frozen=True, slots=True)
class TagResolution:
    """`POST /api/location-tags/resolve`, as the station uses it.

    `disagreement` is passed through untouched: the tag's payload names one slot
    and its UID is bound to another, and only a human at the drawers can say which
    is right. The agent neither resolves it nor blocks on it.
    """

    status: str
    matched_by: str
    location_id: int | None
    disagreement: bool

    @property
    def is_resolved(self) -> bool:
        return self.status == "resolved" and self.location_id is not None


@dataclass(frozen=True, slots=True)
class Movement:
    """A committed movement: the ledger rows it appended, and the new balance.

    `replayed` is the API telling us this `client_op_id` had already been applied
    and it handed back the stored response. That is a success, not an error — it is
    the whole point of minting the key before the user acts.
    """

    seqs: tuple[int, ...]
    replayed: bool
    lot: LotView


class StationApi(Protocol):
    """The three calls a station session makes. Fake it in tests; that is the point."""

    async def resolve_tag(self, *, tag_uid: str | None, ndef_url: str | None) -> TagResolution: ...

    async def read_container(self, location_id: int) -> ContainerView: ...

    async def commit(
        self, *, kind: ActionKind, lot_id: int, qty_milli: int, client_op_id: str
    ) -> Movement: ...


# ---------------------------------------------------------------------------
# Parsing — narrow, and loud about a shape it does not recognise
# ---------------------------------------------------------------------------


def _malformed(what: str) -> ApiError:
    return ApiError(
        f"the API returned something this agent cannot read: {what}",
        status=None,
        reason="malformed_response",
    )


def _mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _malformed(f"{what} is not an object")
    return value


def _int(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    # `bool` is an `int` in Python and would sail through here as 0 or 1.
    if not isinstance(value, int) or isinstance(value, bool):
        raise _malformed(f"{key} is not an integer")
    return value


def _str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise _malformed(f"{key} is not a string")
    return value


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _malformed(f"{key} is neither a string nor null")


def _bool(body: dict[str, Any], key: str) -> bool:
    value = body.get(key)
    if not isinstance(value, bool):
        raise _malformed(f"{key} is not a boolean")
    return value


def lot_view(body: dict[str, Any]) -> LotView:
    return LotView(
        lot_id=_int(body, "id"),
        part_id=_int(body, "part_id"),
        qty_milli=_int(body, "qty_milli"),
        qty_reserved_milli=_int(body, "qty_reserved_milli"),
        status=_str(body, "status"),
        batch_code=_opt_str(body, "batch_code"),
    )


def container_view(body: dict[str, Any]) -> ContainerView:
    lots = body.get("lots")
    if not isinstance(lots, list):
        raise _malformed("lots is not a list")
    return ContainerView(
        location_id=_int(body, "id"),
        name=_str(body, "name"),
        label_path=_str(body, "label_path"),
        short_id=_opt_str(body, "short_id"),
        lots=tuple(lot_view(_mapping(lot, "a lot")) for lot in lots),
    )


def tag_resolution(body: dict[str, Any]) -> TagResolution:
    location = body.get("location")
    return TagResolution(
        status=_str(body, "status"),
        matched_by=_str(body, "matched_by"),
        location_id=None
        if location is None
        else _int(_mapping(location, "location"), "location_id"),
        disagreement=_bool(body, "disagreement"),
    )


def movement(body: dict[str, Any]) -> Movement:
    seqs = body.get("seqs")
    if not isinstance(seqs, list) or not all(
        isinstance(seq, int) and not isinstance(seq, bool) for seq in seqs
    ):
        raise _malformed("seqs is not a list of integers")
    return Movement(
        seqs=tuple(seqs),
        # `replayed` is `ReplayableResponse`'s field and defaults False server-side;
        # treating a missing one as False keeps an older API readable, where
        # demanding it would turn a compatible response into a hard failure.
        replayed=bool(body.get("replayed", False)),
        lot=lot_view(_mapping(body.get("lot"), "lot")),
    )


def _error_from(status: int, raw: bytes) -> ApiError:
    """Turn an HTTP error body into a `reason` the station can display.

    The API's own convention is `{"detail": {"reason", "message"}}` for anything
    a client is expected to act on, and FastAPI's validation errors are a *list*
    under the same key. Both are handled, and anything else degrades to the
    status code rather than raising while raising.
    """
    reason = f"http_{status}"
    message = raw.decode("utf-8", "replace")[:500]
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ApiError(message, status=status, reason=reason)
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if isinstance(detail, dict):
        reason = str(detail.get("reason", reason))
        message = str(detail.get("message", message))
    elif isinstance(detail, str):
        message = detail
    elif detail is not None:
        message = json.dumps(detail)[:500]
    return ApiError(message, status=status, reason=reason)


class HttpStationApi:
    """`StationApi` over HTTP. The only component that talks to the cluster.

    `base_url` is the API as reachable *from the Pi*, which is not the same string
    as `ALMAGEST_BASE_URL`: that one is the public origin written into tags
    (`https://almagest.aether.lan`, ADR 0001) and it must stay stamped on physical
    objects whatever the agent's route to the API happens to be.

    TLS is verified with the system trust store and there is deliberately no
    switch to turn that off. `https://almagest.aether.lan` needs the project's private CA
    installed on the Pi (ADR 0001 makes that a prerequisite anyway, for Web NFC),
    and an `insecure=true` flag is the kind of thing that stays on.
    """

    def __init__(
        self,
        base_url: str,
        *,
        device_id: str,
        timeout_s: float = 5.0,
        source: str = DEFAULT_LEDGER_SOURCE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._device_id = device_id
        self._timeout_s = timeout_s
        self._source = source

    async def resolve_tag(self, *, tag_uid: str | None, ndef_url: str | None) -> TagResolution:
        """Ask the server what this tag is. **Both carriers, always.**

        Sending only the winner would hide the mis-binding the verification walk
        exists to find: the server needs both to report `disagreement`.
        """
        body: dict[str, Any] = {}
        if tag_uid:
            body["tag_uid"] = tag_uid
        if ndef_url:
            body["ndef_url"] = ndef_url
        if not body:
            # The route refuses this with a 422, and going to the network to find
            # that out would be a round trip to learn something local.
            raise ApiError(
                "a tag with neither a UID nor an NDEF record cannot be resolved",
                status=None,
                reason="no_carrier",
            )
        return tag_resolution(await self._request("POST", RESOLVE_PATH, body))

    async def read_container(self, location_id: int) -> ContainerView:
        path = LOCATION_PATH.format(location_id=location_id)
        return container_view(await self._request("GET", path, None))

    async def commit(
        self, *, kind: ActionKind, lot_id: int, qty_milli: int, client_op_id: str
    ) -> Movement:
        """Append the movement. Idempotent on `client_op_id`, by the API's design.

        The key is minted before the user acts, so a retry after a lost response
        replays the stored answer instead of moving stock twice — on an
        append-only ledger a duplicate can only be corrected by writing a third
        row.
        """
        route = _ROUTES[kind]
        body: dict[str, Any] = {
            route.qty_field: qty_milli,
            "client_op_id": client_op_id,
            "device_id": self._device_id,
            "source": self._source,
        }
        path = route.path.format(lot_id=lot_id)
        return movement(await self._request("POST", path, body))

    async def _request(self, method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        return await asyncio.to_thread(self._blocking_request, method, path, body)

    def _blocking_request(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        """One request, in a worker thread. Never raises anything but our two types.

        Every `OSError` becomes `ApiUnavailable` — a refused connection, a DNS
        failure, a TLS handshake and a timeout are all "no answer" from the
        station's point of view, and the one distinction that matters (an answer
        that says no) is `HTTPError`, which is caught first.
        """
        # The scheme is pinned to http(s) by `AgentSettings._require_an_http_url`,
        # so `urlopen` cannot be steered at a `file://` or `ftp://` URL from config.
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Accept": "application/json"}
            | ({} if body is None else {"Content-Type": "application/json"}),
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise _error_from(error.code, error.read()) from error
        except (OSError, TimeoutError) as error:
            # `URLError` is an `OSError`, and so is `socket.timeout`.
            raise ApiUnavailable(f"{method} {path}: {error}") from error
        try:
            return _mapping(json.loads(raw), "the response body")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise _malformed("the response body is not JSON") from error
