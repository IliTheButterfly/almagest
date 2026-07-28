"""Scan resolution: the ordered handler chain, and the aliases it learns.

`POST /api/scan/resolve` is one endpoint with **ordered handlers and first match
wins** — internal short ID, then `barcode_aliases`, then ECIA/MH10.8.2, then
LCSC, then a bare-MPN heuristic, then EAN/UPC, then unknown. The order is the
specification, not an implementation detail: moving the alias step would mean a
binding the user taught could be overruled by a parser's guess.

Nothing here ever rejects a payload. An unrecognised code comes back with
`suggest_bind`, the user says what it is once via `POST /api/scan/alias`, and
from then on it resolves at step 2 forever. That loop is the feature — it
generalises a self-minted-QR scheme to arbitrary vendor payloads, and it is why
a handler this system has not written yet costs nothing but a few taps.
"""

from __future__ import annotations

from app.services.scanning.describe import EntityDescription, describe
from app.services.scanning.resolver import (
    HANDLER_ORDER,
    HANDLERS,
    Candidate,
    ExistingLot,
    ParsedFields,
    ScanResolution,
    record_bind,
    resolve,
)

__all__ = [
    "HANDLERS",
    "HANDLER_ORDER",
    "Candidate",
    "EntityDescription",
    "ExistingLot",
    "ParsedFields",
    "ScanResolution",
    "describe",
    "record_bind",
    "resolve",
]
