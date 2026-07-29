"""What a batch write route needs and a single-object route does not.

ADR 0007's cart drains to one of three destinations, and all three share one
rule: **a line whose stock has moved fails that line and not the batch.** So a
batch route cannot express a per-line refusal the way every other route does —
by raising an `HTTPException` — because that would 4xx the whole request and
leave the client unable to say which row to fix.

`LineRefused` is the replacement, and it lives here rather than in one of the
three route modules so all three raise the same thing. `app.api.idempotency`'s
`LineIdempotencyError` is its sibling: same reason, different subject.
"""

from __future__ import annotations


class LineRefused(Exception):
    """This line cannot be applied. The batch keeps going and reports it.

    `reason` is machine-readable, drawn from the same vocabulary as
    `ledger.LedgerError.reason` and `reservations.ReservationError.reason` — a
    client reading a batch result should not have to learn a second dialect for
    the failures only a batch can have (a lot that has moved out of the scanned
    container, a part deleted since the cart captured it).
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason
