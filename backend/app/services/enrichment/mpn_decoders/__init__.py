"""Reading a part's specification out of its own part number.

Pure functions, no I/O, no API key, no model — and they are the reason a fresh
install is useful before any provider is configured. `GRM188R71H104KA93D` yields a
dielectric, a voltage rating, a size, a capacitance and a tolerance from the
string alone, which is most of what a parametric search needs.

## Why only five families, and why that is not a gap

**There is no generic capacitor decoder, anywhere, because there is no generic
scheme to decode.** Every manufacturer's numbering is private, the fields are in a
different order with different widths, and the same two characters mean a
dielectric to one vendor and a case size to another. The only way to decode any of
it is to read that manufacturer's own catalogue and transcribe its tables, which is
what was done here for Murata GRM, Samsung CL, Yageo CC and RC, and TDK C — chosen
because between them they cover the overwhelming majority of the passives a
hobby-scale inventory actually contains.

So the list is short on purpose. **Do not "complete" this module by pattern-
matching an unfamiliar part number against the families below.** A decoder built on
a resemblance produces a confident dielectric or voltage rating for a part it has
never seen, that value flows into a substitution decision, and substitution in this
system is only correct by construction if its inputs are true. Adding a family
means finding the manufacturer's numbering table and transcribing it, with the
source cited in the module, exactly as the four families here do. Anything else is
worse than having no decoder: no decoder sends the part to the review queue, where a
human reads the datasheet.

## Resolution order

`decode()` walks `REGISTRY`, which is sorted by **descending prefix length**, and
the first family whose prefix matches gets the number. So `CL10B104KB8NNNC` is
Samsung's (`cl`) and never TDK's (`c`), and the ordering is a property of the data
rather than of dict iteration or of the order somebody happened to write the
imports. Equal-length prefixes cannot both match one string, so ties among them are
irrelevant.

**The first matching family's answer is final** — there is no fall-through to a
shorter prefix when a decoder returns `None`. That is deliberate: a Samsung number
with a mangled body must come back as "not decoded", not be handed to TDK's decoder
to produce a plausible-looking capacitor out of Samsung's field layout.

The generic resistor markings are registered with the **empty prefix**, so they are
last by construction. Read `smd_resistor` before using their results: they are
markings rather than part numbers, and are flagged `is_marking`.

## Feeding the results to `parameter_value`

`DecodedPart.parameters` is keyed by `parameter_template.name`, and its values are
raw strings for `services.parameters` — numerics carry their unit (`"100 nF ±10%"`,
`"50 V"`, `"10 kohm ±1%"`) so `set_numeric` parses them with the same grammar as
typed input and populates `value_min`/`value_max`; enum facets carry a choice key or
alias (`"X7R"`, `"0603"`, `"SMD"`). Units are never implied by the key.

Provenance for anything written from here is `Provenance.MPN_DECODER`, which sits
below `datasheet_table` and above `distributor_freetext`: a decoded field is exactly
as trustworthy as the manufacturer's numbering scheme, which is to say very, but a
printed table in the datasheet still wins.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.services.scanning.codes import normalize_mpn

from . import murata, samsung, smd_resistor, tdk, yageo
from ._result import DecodedPart

__all__ = ["REGISTRY", "DecodedPart", "Family", "decode"]


@dataclass(frozen=True)
class Family:
    """One numbering scheme and the prefix that identifies it."""

    name: str

    #: Matched against the **normalised** part number, so it is lower-case and
    #: free of hyphens and spaces. Empty means "matches anything", which sorts
    #: last.
    prefix: str

    decode: Callable[[str], DecodedPart | None]


#: Declaration order is presentation only — `REGISTRY` below imposes the order
#: that matters.
_FAMILIES: tuple[Family, ...] = (
    Family(name=murata.FAMILY, prefix="grm", decode=murata.decode),
    Family(name=samsung.FAMILY, prefix="cl", decode=samsung.decode),
    Family(name=yageo.CC_FAMILY, prefix="cc", decode=yageo.decode_cc),
    Family(name=yageo.RC_FAMILY, prefix="rc", decode=yageo.decode_rc),
    Family(name=tdk.FAMILY, prefix="c", decode=tdk.decode),
    Family(name=smd_resistor.FAMILY, prefix="", decode=smd_resistor.decode),
)

#: Most specific prefix first. Sorted once here so no caller and no future family
#: can make resolution depend on declaration order.
REGISTRY: tuple[Family, ...] = tuple(sorted(_FAMILIES, key=lambda family: -len(family.prefix)))


def decode(mpn: str) -> DecodedPart | None:
    """Decode `mpn` with the most specific family that claims its prefix.

    `None` means no family recognised it — the ordinary case for the great
    majority of part numbers, and the one that sends a part to enrichment's other
    sources instead.
    """
    key = normalize_mpn(mpn)
    if not key:
        return None

    for family in REGISTRY:
        if key.startswith(family.prefix):
            return family.decode(key)
    return None
