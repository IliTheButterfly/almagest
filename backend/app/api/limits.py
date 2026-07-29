"""Bounded integer types for request bodies.

Every quantity, money and mass field in the API uses one of these rather than a
bare `int`. That is not tidiness — a bare `int` in Pydantic has **no upper
bound**, so `{"qty_milli": 10**30}` validates cleanly, reaches
`session.flush()`, and dies in sqlite3's parameter binding with
``OverflowError: Python int too large to convert to SQLite INTEGER``. Nothing
catches it, so the client gets a bare 500 for input that should obviously be a
422 — and an adversarial or simply corrupted barcode ``Q`` (quantity) field
pre-filling an intake form is enough to produce it.

The bounds are **domain** bounds, not storage bounds, chosen well inside
SQLite's signed 64-bit range for a specific reason: a cached balance
*accumulates*. Capping a single write at SQLite's own maximum would still let a
few thousand writes overflow `stock_lots.qty_milli_cached`, which is a silent
corruption rather than a clean rejection. Nine orders of magnitude of headroom
means the accumulated balance cannot realistically get there.

They are also deliberately absurd relative to real inventory — a billion of
anything, a billion currency units, a tonne of one part. The point is to catch
input that is obviously not a quantity, not to second-guess a user with a big
reel.

Defining them here rather than per field is the actual fix. The original bug was
not one missing constraint; it was eight fields each inventing their own, so any
ninth would have inherited the gap.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

#: SQLite's signed 64-bit maximum. Anything above this cannot be *bound* as a
#: query parameter at all, so it is the correct ceiling for a row id — unlike a
#: quantity, an id never accumulates, so there is nothing to leave headroom for.
ROW_ID_MAX = 2**63 - 1

#: 10^12 milli-units — a billion whole units of anything.
QTY_MILLI_MAX = 10**12

#: 10^15 micro — a billion currency units.
MONEY_MICRO_MAX = 10**15

#: 10^12 mg — one tonne. The bench scale tops out around 100 g.
MASS_MG_MAX = 10**12

#: A reference to a row, in a body or a path.
#:
#: Row ids need the same treatment as quantities, and for the same reason: a
#: `part_id` of 10**30 reaches `Session.get()`, which binds it as a parameter and
#: raises `OverflowError` before any "not found" check can run. That produced a
#: 500 on `POST /api/stock/receive`, `GET /api/parts/{id}` and
#: `POST /api/locations/suggest`. `ge=1` additionally turns a nonsense id like 0
#: or -1 into a 422 rather than a lookup that was always going to miss.
RowId = Annotated[int, Field(ge=1, le=ROW_ID_MAX)]

#: A strictly positive quantity: a receipt, a take, a return.
QtyMilli = Annotated[int, Field(gt=0, le=QTY_MILLI_MAX)]

#: A signed correction. Zero is rejected in the service layer rather than here,
#: because "an adjustment of nothing" is a domain refusal with its own reason
#: code, not a schema violation.
DeltaMilli = Annotated[int, Field(ge=-QTY_MILLI_MAX, le=QTY_MILLI_MAX)]

#: The result of physically counting something, so zero is legitimate.
CountMilli = Annotated[int, Field(ge=0, le=QTY_MILLI_MAX)]

#: Money is stored as micro-units of the currency, per the schema conventions.
MoneyMicro = Annotated[int, Field(ge=0, le=MONEY_MICRO_MAX)]

#: A measured mass in milligrams, from the bench scale or a tare.
MassMg = Annotated[int, Field(ge=0, le=MASS_MG_MAX)]

#: A grid dimension or footprint — rows, cols, a merge's row/col span, a
#: height in units. Nothing in the layout editor's domain is anywhere near
#: this large; the bound exists so a typo'd cell count fails as a 422 rather
#: than as a request to generate that many `container_type_slot_templates` or
#: `locations` rows in one call.
GridSpan = Annotated[int, Field(ge=1, le=10_000)]

#: A 0-based row/col position in a grid, so it must admit zero unlike `GridSpan`.
GridIndex = Annotated[int, Field(ge=0, le=10_000)]

#: How many sibling containers `instantiate` creates in one call. A cabinet
#: run is tens, not thousands; the bound catches a fat-fingered count before
#: it tries to materialise that many location trees in one request.
InstanceCount = Annotated[int, Field(ge=1, le=1_000)]

#: Print resolution in dots per inch. 72 is below anything legible; 1200 is
#: past what a laser printer offers. Not a hardware ceiling — a sanity bound
#: against a typo'd zero or a client confusing dots-per-inch with dots-per-mm.
LabelDpi = Annotated[int, Field(ge=72, le=1200)]

#: How many assemblies one `ProjectBuild` makes. Not a `*_milli` quantity —
#: `project_builds.assembly_count` is a plain `Integer`, a count of boards, not
#: a physical amount of a part — so it gets its own bound rather than borrowing
#: `QtyMilli`'s headroom, which is calibrated for an accumulating cache instead
#: of a single plan-time field. A production run of thousands of boards is
#: nowhere near this; the bound exists to catch a fat-fingered count before it
#: is multiplied through every BOM line's demand.
AssemblyCount = Annotated[int, Field(ge=1, le=100_000)]

#: How far into a result set a page may start. Unbounded it is the same bug this
#: module was written for, one layer further out: `offset=10**30` never reaches a
#: domain check, it reaches SQLite's `LIMIT ... OFFSET` bind and raises
#: `OverflowError`, so a query string produced a bare 500. A million rows deep is
#: past any paging a human does — the honest answer to "page 20 million" is that
#: the query was wrong, and a 422 says so.
ResultOffset = Annotated[int, Field(ge=0, le=1_000_000)]

#: How many documents one extraction worker takes in a single claim. Small on
#: purpose: every claimed document holds a lease, so a worker that grabs a hundred
#: and then dies parks all hundred until the lease expires. Batching exists to
#: amortise the round trip, not to reserve the queue.
ClaimLimit = Annotated[int, Field(ge=1, le=50)]

#: Where a row sorts in a hand-ordered list — a part kind in the kind picker, a
#: filterable field in the filter panel, one option inside a list field. Signed,
#: because "put this one first" is naturally expressed as a negative rather than
#: by renumbering everything else. Bounded for the reason this whole module
#: exists: `sort_order` is a plain `Integer`, and a pasted 10**30 reaches
#: `session.flush()` and dies in sqlite3's parameter binding as a bare 500.
SortOrder = Annotated[int, Field(ge=-1_000_000, le=1_000_000)]

#: How many candidate parts one requirement is answered with, per availability
#: list. Bounded low on purpose rather than for safety: a suggestion is read by a
#: human, and fifty options is not a shortlist, it is the search results — which
#: `/api/search/parts` already serves, with paging and a true total.
CandidateLimit = Annotated[int, Field(ge=1, le=50)]

__all__ = [
    "MASS_MG_MAX",
    "MONEY_MICRO_MAX",
    "QTY_MILLI_MAX",
    "AssemblyCount",
    "CandidateLimit",
    "ClaimLimit",
    "CountMilli",
    "DeltaMilli",
    "GridIndex",
    "GridSpan",
    "InstanceCount",
    "LabelDpi",
    "MassMg",
    "MoneyMicro",
    "QtyMilli",
    "ResultOffset",
    "SortOrder",
]
