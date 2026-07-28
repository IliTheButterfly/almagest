"""Adapter between the `ecia-barcode` library and the resolver chain.

The library is standalone and knows nothing about this schema — the same split
as `services/search/value_parser.py` and the value parser. It supplies the
envelope grammar and the DI table; this module supplies the two things it
cannot: *is this payload even an MH10.8.2 label*, and *which of its fields could
name a part we hold*.

The first question is not rhetorical. The library's contract is "degrades, never
raises", so `parse()` returns a label for literally any input, and the DI table
contains real one- and two-character codes — `4K` is purchase order, `P` is
customer part number, `S` is serial. Feed it the bare short ID `4K7T92M8` and it
happily reports a purchase order of `7T92M8`. That makes the library's output
useless as a format test, so the test lives here instead, and it tests for
*structure*: the `[)>` envelope, or at minimum one GS separator proving the
payload is a separated multi-field record rather than a word.
"""

from __future__ import annotations

from ecia_barcode import EciaLabel
from ecia_barcode import parse as parse_label

#: Group Separator. One of these is the weakest structural evidence worth
#: accepting; see the module docstring.
GS = "\x1d"

#: The well-formed opener, and Mouser's malformed variant with a stray leading
#: `>` which the library strips and penalises.
_ENVELOPES = ("[)>", ">[)>")


def looks_like_ecia(payload: str) -> bool:
    """Whether `payload` has the shape of an MH10.8.2 label at all.

    Structure only — nothing about whether the fields mean anything. A payload
    with the envelope is asserting the format; a payload with a GS in it is at
    least a separated record. Neither is something arbitrary vendor text does by
    accident, and requiring one of them is what stops this handler claiming a
    short ID, a bare MPN or an EAN out from under the steps that own them.
    """
    return payload.startswith(_ENVELOPES) or GS in payload


def parse(payload: str) -> EciaLabel | None:
    """The parsed label, or `None` if this is not an ECIA payload.

    `None` means "not my format, try the next handler". A label with an empty
    field map is also `None`: the envelope may have survived a cropped scan while
    every field was lost, and a handler that claims a payload it extracted
    nothing from would stop the chain for no benefit.
    """
    if not looks_like_ecia(payload):
        return None
    label = parse_label(payload)
    if not label.fields:
        return None
    return label


def mpn_candidates(label: EciaLabel) -> tuple[str, ...]:
    """Field values that could be a manufacturer part number, in preference
    order and deduplicated.

    Both `P` (customer part number) and `1P` (supplier part number) are returned
    because **distributors disagree about which one carries the manufacturer's
    part number**, and no marker on the label says which convention was used.
    Picking one would be a guess that silently fails for half the suppliers;
    trying both and unioning the matches is the honest option. If they match two
    different parts the resolution comes back `ambiguous`, which asks the user —
    the correct outcome for a genuinely undetermined label.
    """
    ordered = (label.customer_part_number, label.supplier_part_number)
    seen: dict[str, None] = {}
    for value in ordered:
        if value:
            seen.setdefault(value.strip(), None)
    return tuple(seen)


def quantity_milli(label: EciaLabel) -> int | None:
    """DI `Q` as a milli-unit count.

    `Q` on a component label is a whole number of pieces, and quantities in this
    schema are thousandths of the part's unit of measure, so the conversion is
    exact and the ledger stays summable without rounding. `None` when `Q` is
    absent or not a whole number — the raw string is still in `fields["Q"]`.
    """
    quantity = label.quantity
    return None if quantity is None else quantity * 1000
