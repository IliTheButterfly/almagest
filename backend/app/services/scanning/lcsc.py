"""Step 4 of the resolver chain: LCSC's proprietary label format. **Not
implemented, on purpose.**

LCSC labels are not MH10.8.2-compliant, so the ECIA parser at step 3 cannot read
them and the design says this handler must be reverse-engineered *from samples*.
We have no samples. That is the entire reason there is no code here.

Guessing the format would be the worst available option, worse than this stub by
a wide margin. A handler built on a plausible-looking pattern does not fail
loudly — it claims payloads it half-understands and returns a **confidently
wrong part identification**, which then becomes a stock movement against the
wrong part, in an append-only ledger, discovered weeks later when a bin does not
match its count. Returning `None` costs one extra tap: the payload falls through
to `unknown`, the user binds it once, and `barcode_aliases` resolves that exact
label forever after. The alias-learning loop is precisely what makes having no
LCSC parser survivable.

## What is needed to implement this

1. **Real payloads.** `scan_events` already keeps every scan verbatim, control
   characters intact, which is what makes them minable later::

       SELECT raw_payload, symbology, COUNT(*) AS seen
         FROM scan_events
        WHERE decoded_kind = 'unknown'
        GROUP BY raw_payload
        ORDER BY seen DESC;

   Collect from both carriers LCSC actually uses — the bag QR and the reel
   label — and from several order batches, since a format inferred from one
   shipment is a format inferred from one printer configuration.
2. **A ground-truth pairing per sample**, i.e. what the label physically said,
   written down by hand. `tests/fixtures/ecia/*.expected.json` is the pattern:
   with no reference implementation to diff against, hand-verified fixtures
   *are* the specification.
3. **Only then** the parser, plus the same "degrades, never raises" contract the
   ECIA library follows, and a confidence score low enough that a partial parse
   routes to review rather than to the ledger.

Until then this module exists so the chain's shape matches the design and the
gap is documented where someone will actually read it.
"""

from __future__ import annotations

#: Flipped to `True` by whoever implements the parser. A constant rather than an
#: inference from behaviour, so enabling this handler is a deliberate, reviewable
#: edit — and so the test asserting the stub claims nothing fails at the moment
#: the claim becomes real, instead of silently passing on an empty function.
SUPPORTED = False


def parse(payload: str) -> None:
    """Always `None` — "not my format, try the next handler".

    The signature is the one every other handler adapter uses, so implementing
    this later is filling in a body rather than rewiring the chain.
    """
    # `payload` is deliberately unused. Referencing it would be the first step
    # toward a heuristic, and the module docstring explains why there must not
    # be one until there are samples.
    del payload
    return None
