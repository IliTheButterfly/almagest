"""Samsung Electro-Mechanics CL series — multilayer ceramic capacitors.

The field layout is printed on Samsung's own specification sheets, which decode
one part number position by position:

    CL   10   B   104   K   B   8   N   N   N   C
    ①    ②    ③   ④     ⑤   ⑥   ⑦   ⑧   ⑨   ⑩   ⑪

    ① series   ② size   ③ dielectric   ④ capacitance   ⑤ capacitance tolerance
    ⑥ rated voltage   ⑦ thickness   ⑧ inner electrode / termination / plating
    ⑨ product   ⑩ special (reserved)   ⑪ packaging

**Samsung publishes no consolidated code table that could be obtained for this
work**, unlike Murata and TDK. So each entry in the tables below is here because a
Samsung document decodes it, or because a real catalogued part pins it:

    size 05 = 0402, dielectric C = C0G, tolerance C = ±0.25 pF,
    voltage B = 50 V, thickness 5 = 0.50 mm      CL05C6R8CB5NNNC spec sheet
    size 10 = 0603, dielectric B = X7R,
    voltage B = 50 V, thickness 8 = 0.80 mm      CL10B104KB8NNNC spec sheet
    dielectric A = X5R, voltage P = 10 V         Samsung, "The Rules behind
                                                 MLCC Part Numbers"
    size 21 = 0805, voltage O = 16 V             CL21A106KOFNNNE, 10 µF 16 V 0805
    size 31 = 1206, voltage C = 100 V            CL31B105KCHNNNE, 1 µF 100 V 1206
    voltage Q = 6.3 V                            CL10A106MQ8NNNC, 10 µF 6.3 V
    voltage A = 25 V                             CL31A106KAHNNNE, 10 µF 25 V

Widely-repeated third-party tables add more rows — `R` = 4 V, `L` = 35 V,
`D` = 200 V, sizes `02`/`03`/`32`/`43`, thicknesses `A`/`D`/`H` — and they are
**deliberately not included**. Every one of them is plausible and none is
confirmed, and an unconfirmed voltage rating is the single worst thing this
decoder could emit: substitution search is correct by construction only if its
inputs are. A code that is absent produces a partial decode naming the field,
which is a review-queue item; a code that is wrong produces a part that looks
right and fails in a circuit. Filling these in is a documentation task, not a
coding one.

⑧⑨⑩ are not decoded: the positions are named by Samsung but only the value `N`
appears in the sheets, so there is no table to apply.
"""

from __future__ import annotations

import re

from . import _eia
from ._result import DecodedPart

FAMILY = "samsung_cl"

#: The trailing block ⑧⑨⑩⑪ is three or four characters because Samsung's own
#: component library indexes numbers without the packaging character
#: (`CL10B104KB8NNN`), and distributors sell them with it.
_PATTERN = re.compile(
    r"^cl"
    r"(?P<size>\d{2})"
    r"(?P<dielectric>[a-z])"
    r"(?P<capacitance>[0-9r]{3})"
    r"(?P<tolerance>[a-z])"
    r"(?P<voltage>[a-z])"
    r"(?P<thickness>[0-9a-z])"
    r"(?P<tail>[0-9a-z]{3,4})$"
)

#: ② Size. Values are the EIA imperial code, which is also how Samsung's sheets
#: name it ("0603 (inch code)").
_SIZE: dict[str, str] = {
    "05": "0402",
    "10": "0603",
    "21": "0805",
    "31": "1206",
}

#: ③ Dielectric. Samsung prints the EIA designation for each of these, so they
#: populate `dielectric` directly.
_DIELECTRIC: dict[str, str] = {
    "A": "X5R",
    "B": "X7R",
    "C": "C0G",
}

#: ⑥ Rated voltage, in volts DC.
_VOLTAGE_V: dict[str, str] = {
    "Q": "6.3",
    "P": "10",
    "O": "16",
    "A": "25",
    "B": "50",
    "C": "100",
}

#: ⑦ Thickness, in millimetres.
_THICKNESS_MM: dict[str, str] = {
    "5": "0.5",
    "8": "0.8",
}

#: ⑪ Packaging. One row, because one row is what a Samsung document pins: the
#: `CL10B104KB8NNNC` and `CL05C6R8CB5NNNC` sheets both end in `C`, and Samsung's
#: catalogue names that "cardboard tape (paper), normal, 7 inch reel".
#:
#: `B` = "bulk" was here and is deleted. It is in no Samsung packaging table and
#: the word "bulk" is not in the catalogue at all — it is the plausible default
#: this module's own preamble forbids, and a mistyped or OCR'd final character
#: would have decoded to it instead of naming the field unread.
_PACKAGING: dict[str, str] = {
    "C": "cardboard tape, 7 inch reel",
}


def decode(normalized_mpn: str) -> DecodedPart | None:
    """Decode a normalised CL part number, or `None` if it is not one."""
    match = _PATTERN.match(normalized_mpn)
    if match is None:
        return None

    parameters: dict[str, str] = {"mounting_type": "SMD", "capacitor_technology": "ceramic"}
    extras: dict[str, str] = {"manufacturer": "Samsung Electro-Mechanics"}
    unknown: list[str] = []

    package = _SIZE.get(match["size"])
    if package is None:
        unknown.append("size")
    else:
        parameters["package"] = package

    dielectric = _DIELECTRIC.get(match["dielectric"].upper())
    if dielectric is None:
        unknown.append("dielectric")
    else:
        parameters["dielectric"] = dielectric
        extras["temperature_characteristic"] = dielectric

    voltage = _VOLTAGE_V.get(match["voltage"].upper())
    if voltage is None:
        unknown.append("rated_voltage")
    else:
        parameters["voltage_rating"] = f"{voltage} V"

    thickness = _THICKNESS_MM.get(match["thickness"].upper())
    if thickness is None:
        unknown.append("thickness")
    else:
        extras["thickness_mm"] = thickness

    picofarads = _eia.capacitance_pf(match["capacitance"])
    tolerance = _eia.tolerance_of(match["tolerance"], picofarads)
    _eia.apply_capacitance(parameters, extras, unknown, picofarads, tolerance)

    tail = match["tail"]
    if len(tail) == 4:
        described = _PACKAGING.get(tail[3].upper())
        if described is None:
            unknown.append("packaging")
        else:
            extras["packaging"] = described

    return DecodedPart(family=FAMILY, parameters=parameters, extras=extras, unknown=tuple(unknown))
