"""Murata GRM — chip monolithic ceramic capacitors.

Every table here is transcribed from **Murata Cat.No.C02E-16, "Chip Monolithic
Ceramic Capacitors", pp. 2–5**, which prints the numbering scheme field by field:

    GR   M   18   8   B1   1H   102   K   A01   D
    ①    ②   ③    ④   ⑤    ⑥    ⑦     ⑧   ⑨     ⑩

    ① product ID   ② series   ③ dimensions L×W   ④ dimension T
    ⑤ temperature characteristics   ⑥ rated voltage   ⑦ capacitance
    ⑧ capacitance tolerance   ⑨ individual specification code   ⑩ packaging

Only `GRM` is registered, although ①②③④⑤⑥⑦⑧ are shared with Murata's other MLCC
series (GRT, GRJ, GCM, GJM …). Registering the ones we have not read the
catalogue rows for would be a guess dressed as coverage; adding a series later is
one more entry in the registry.

**⑨ is never decoded.** The catalogue's entire description of it is "expressed by
three figures" — it is an internal specification id, and the only thing that could
be produced from it is invention. ⑩ *is* decoded: Murata prints the table.

The dimensions codes look derivable — first significant digit of L, then of W —
and that is exactly why the literal table is used instead. `0D` (0.38 × 0.38 mm)
and `0M` (0.9 × 0.6 mm) do not follow the pattern, so a decoder built on the
pattern would be right about fifteen sizes and confidently wrong about three.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import _eia
from ._result import DecodedPart

FAMILY = "murata_grm"

#: All groups are fixed-width, which is what makes the split unambiguous. The
#: tail (⑨⑩) is optional because Murata's own catalogue tables, and every
#: distributor, quote GRM numbers both with and without it.
_PATTERN = re.compile(
    r"^grm"
    r"(?P<dims>[0-9a-z]{2})"
    r"(?P<thickness>[0-9a-z])"
    r"(?P<temp_char>[0-9a-z]{2})"
    r"(?P<voltage>[0-9a-z]{2})"
    r"(?P<capacitance>[0-9r]{3})"
    r"(?P<tolerance>[a-z])"
    r"(?:(?P<spec>[0-9a-z]{3})(?P<packaging>[a-z])?)?$"
)

#: Dimensions (L x W), field 3, catalogue p.2. Values are the EIA **imperial** code,
#: because that is the primary spelling of the `package` parameter's choices —
#: emitting the metric one would resolve `0603` to the imperial 0603 choice and
#: silently file a 0201 part as a 0603.
_DIMENSIONS: dict[str, str] = {
    "02": "01005",
    "03": "0201",
    "05": "0202",
    "08": "0303",
    "0D": "015015",
    "0M": "0302",
    "15": "0402",
    "18": "0603",
    "1M": "0504",
    "21": "0805",
    "22": "1111",
    "31": "1206",
    "32": "1210",
    "42": "1808",
    "43": "1812",
    "52": "2211",
    "55": "2220",
}

#: ④ Dimension (T), catalogue p.2, in millimetres. `9` really is 0.85 mm and not
#: 0.9 — one of the reasons this is a table and not arithmetic. `X` means "depends
#: on individual standards", i.e. undecodable, so it is absent by design.
_THICKNESS_MM: dict[str, str] = {
    "2": "0.2",
    "3": "0.3",
    "5": "0.5",
    "6": "0.6",
    "7": "0.7",
    "8": "0.8",
    "9": "0.85",
    "A": "1.0",
    "B": "1.25",
    "C": "1.6",
    "D": "2.0",
    "E": "2.5",
    "F": "3.2",
    "M": "1.15",
    "N": "1.35",
    "Q": "1.5",
    "R": "1.8",
    "S": "2.8",
}


@dataclass(frozen=True)
class _TempChar:
    """A temperature characteristic and *whose* designation it is.

    The distinction is load-bearing. `dielectric` is a parametric facet whose
    choices are EIA codes, so only an EIA row may populate it; a JIS `B` or a
    Murata-private `X8L` is reported as text instead of being coerced into a
    neighbouring EIA code it is not equal to.
    """

    designation: str

    #: The catalogue's `JIS/EIA` column, or `None` where that column prints `-`.
    #: `None` is not "we could not read it": it is the catalogue stating the
    #: designation belongs to neither standard, so naming one would be an
    #: attribution no document supports — and, since only an EIA row may populate
    #: `dielectric`, an invented `EIA` here would put a code that is not an EIA
    #: dielectric into a parametric facet whose choices are EIA codes.
    standard: str | None


#: ⑤ Temperature characteristics, catalogue p.3. The `Public STD Code` column
#: gives the designation and the `JIS/EIA` column gives the standard; `L8` is
#: footnoted as a Murata code rather than either, and `1X` (`SL`) is the one row
#: whose `JIS/EIA` cell is `-`, so its standard is `None`. `W0` has no public
#: designation at all and is therefore omitted, which makes it an `unknown` field.
_TEMP_CHAR: dict[str, _TempChar] = {
    # Class 1, no standard named in the catalogue's own column.
    "1X": _TempChar("SL", None),
    # Class 1, JIS.
    "2C": _TempChar("CH", "JIS"),
    "2P": _TempChar("PH", "JIS"),
    "2R": _TempChar("RH", "JIS"),
    "2S": _TempChar("SH", "JIS"),
    "2T": _TempChar("TH", "JIS"),
    "3C": _TempChar("CJ", "JIS"),
    "3P": _TempChar("PJ", "JIS"),
    "3R": _TempChar("RJ", "JIS"),
    "3S": _TempChar("SJ", "JIS"),
    "3T": _TempChar("TJ", "JIS"),
    "3U": _TempChar("UJ", "JIS"),
    "4C": _TempChar("CK", "JIS"),
    # Class 1, EIA.
    "5C": _TempChar("C0G", "EIA"),
    "5G": _TempChar("X8G", "EIA"),
    "6C": _TempChar("C0H", "EIA"),
    "6P": _TempChar("P2H", "EIA"),
    "6R": _TempChar("R2H", "EIA"),
    "6S": _TempChar("S2H", "EIA"),
    "6T": _TempChar("T2H", "EIA"),
    "7U": _TempChar("U2J", "EIA"),
    # Class 2, JIS.
    "B1": _TempChar("B", "JIS"),
    "B3": _TempChar("B", "JIS"),
    "F1": _TempChar("F", "JIS"),
    "R1": _TempChar("R", "JIS"),
    "R3": _TempChar("R", "JIS"),
    # Class 2, EIA.
    "C7": _TempChar("X7S", "EIA"),
    "C8": _TempChar("X6S", "EIA"),
    "D7": _TempChar("X7T", "EIA"),
    "D8": _TempChar("X6T", "EIA"),
    "E7": _TempChar("X7U", "EIA"),
    "F5": _TempChar("Y5V", "EIA"),
    "R6": _TempChar("X5R", "EIA"),
    "R7": _TempChar("X7R", "EIA"),
    "R9": _TempChar("X8R", "EIA"),
    # Class 2, Murata's own designation.
    "L8": _TempChar("X8L", "Murata"),
}

#: ⑥ Rated voltage, catalogue p.4: the shared industry codes plus Murata's two
#: non-systematic DC rows. The AC and camera-flash codes (`E2`, `BB`, `GC`, `GF`,
#: `GD`, `GB`) are deliberately absent: they belong to the GJM/GC/GF/GD/GB series,
#: not GRM, and `voltage_rating` is a DC rating — filing "AC250V" under it would
#: make an AC safety capacitor look like a 250 V DC part to substitution search.
_VOLTAGE_V: dict[str, str] = {**_eia.DC_VOLTAGE_V, "YA": "35", "YD": "300"}

#: ⑩ Packaging, catalogue p.5.
_PACKAGING: dict[str, str] = {
    "L": "180 mm reel, embossed taping",
    "D": "180 mm reel, paper taping",
    "E": "180 mm reel, paper taping (LLL15)",
    "K": "330 mm reel, embossed taping",
    "J": "330 mm reel, paper taping",
    "F": "330 mm reel, paper taping (LLL15)",
    "B": "bulk",
    "C": "bulk case",
    "T": "bulk tray",
}


def decode(normalized_mpn: str) -> DecodedPart | None:
    """Decode a normalised GRM part number, or `None` if it is not one."""
    match = _PATTERN.match(normalized_mpn)
    if match is None:
        return None

    parameters: dict[str, str] = {
        # Both facts come from the series code itself: GRM is a *chip* (surface
        # mount) *ceramic* capacitor. Neither is inferred from anything else.
        "mounting_type": "SMD",
        "capacitor_technology": "ceramic",
    }
    extras: dict[str, str] = {"manufacturer": "Murata"}
    unknown: list[str] = []

    package = _DIMENSIONS.get(match["dims"].upper())
    if package is None:
        unknown.append("dimensions")
    else:
        parameters["package"] = package

    thickness = _THICKNESS_MM.get(match["thickness"].upper())
    if thickness is None:
        unknown.append("thickness")
    else:
        extras["thickness_mm"] = thickness

    temp_char = _TEMP_CHAR.get(match["temp_char"].upper())
    if temp_char is None:
        unknown.append("temperature_characteristic")
    else:
        # The parenthesised standard is dropped rather than filled with a
        # placeholder: "SL (none)" reads as a decoded fact about a standard, and
        # the catalogue's `-` is the absence of one.
        extras["temperature_characteristic"] = (
            temp_char.designation
            if temp_char.standard is None
            else f"{temp_char.designation} ({temp_char.standard})"
        )
        if temp_char.standard == "EIA":
            parameters["dielectric"] = temp_char.designation

    voltage = _VOLTAGE_V.get(match["voltage"].upper())
    if voltage is None:
        unknown.append("rated_voltage")
    else:
        parameters["voltage_rating"] = f"{voltage} V"

    picofarads = _eia.capacitance_pf(match["capacitance"])
    tolerance = _eia.tolerance_of(match["tolerance"], picofarads)
    _eia.apply_capacitance(parameters, extras, unknown, picofarads, tolerance)

    packaging = match["packaging"]
    if packaging is not None:
        described = _PACKAGING.get(packaging.upper())
        if described is None:
            unknown.append("packaging")
        else:
            extras["packaging"] = described

    return DecodedPart(family=FAMILY, parameters=parameters, extras=extras, unknown=tuple(unknown))
