"""TDK C series — multilayer ceramic chip capacitors.

Fields and code tables come from **TDK's general MLCC specification, §2.1 "Item
Number Description"**, which prints:

    C   3216   X7R   1C   335   K   T   XXXX
    ①   ②      ③     ④    ⑤     ⑥   ⑦   ⑧

    ① series   ② case size   ③ temperature characteristics   ④ rated voltage
    ⑤ rated capacitance   ⑥ capacitance tolerance   ⑦ packaging
    ⑧ TDK's internal codes

Current TDK numbers carry two extra fields between ⑥ and the tail — a
three-digit thickness in hundredths of a millimetre, then a two-character code —
so `C1608X7R1H104K080AA` is the same eight fields plus `080` (0.80 mm) and `AA`.
The thickness reading is corroborated by TDK's own catalogue entries
(`C2012X7R2A102K085AE` is the 0.85 mm variant, `C3216C0G2J182J115AA` the
1.15 mm one) and by §2.1's own dimension table.

**The trailing two characters are not decoded.** They are a packaging style plus
a code the catalogue itself calls reserved and internal to TDK. The catalogue
tabulates the styles, but they are reel diameters and tape pitches — nothing this
system has a field for — and the reserved half has no public meaning at all, so
the pair is recorded verbatim instead. In the older seven-field form there is
instead a *single* trailing character, ⑦ packaging, and the catalogue's table for
it has exactly one row, `T` = tape and reel, so `T` is the only code decoded.
Which form a number is in is decided by whether the thickness field is present,
not by guessing at the tail.

③ is written out in full on the part number (`X7R`, `C0G`), so it needs no
lookup — but it is still matched against a list of designations rather than "two
or three letters", because that field's width is what tells the rated-voltage
field where it starts. An unrecognised characteristic therefore fails the whole
shape rather than shifting every later field by one and decoding a different
capacitor.
"""

from __future__ import annotations

import re

from . import _eia
from ._result import DecodedPart

FAMILY = "tdk_c"

#: ③ Temperature characteristics. EIA designations that also name a `dielectric`
#: choice, plus the JIS ones TDK prints on Class-1/Class-2 parts. Longest first so
#: the alternation cannot match `CH` inside a three-character code.
_EIA_TEMP_CHAR: frozenset[str] = frozenset(
    {"C0G", "X5R", "X6S", "X6T", "X7R", "X7S", "X7T", "X7U", "X8G", "X8L", "X8R", "Y5V"}
)
_JIS_TEMP_CHAR: frozenset[str] = frozenset({"CG", "CH", "CJ", "CK", "JB", "SL", "UJ"})
_TEMP_CHAR_ALTERNATION = "|".join(
    sorted((*_EIA_TEMP_CHAR, *_JIS_TEMP_CHAR), key=lambda code: (-len(code), code))
).lower()

#: ② Case size, §2.1 Table 2.1, which prints TDK's metric code beside the EIA
#: style (`C1608 (CC0603)`). Values are the **imperial** code for the same reason
#: as everywhere else here: the metric spelling `0603` would resolve to the
#: imperial 0603 choice and misfile a 0201 part.
_CASE_SIZE: dict[str, str] = {
    "0402": "01005",
    "0603": "0201",
    "1005": "0402",
    "1608": "0603",
    "2012": "0805",
    "3216": "1206",
    "3225": "1210",
    "4532": "1812",
    "5750": "2220",
}

#: ⑦ Packaging, §2.1. Only meaningful in the older form — see the module
#: docstring. **One row, because TDK's table has one row.** `B` = "bulk" and
#: `C` = "cassette" were here; neither is in any TDK packaging table, and TDK
#: issues no C-series number ending in either, so nothing genuine ever reached
#: them — but a mistyped or OCR'd last character did, and came back as a
#: confident packaging style rather than as a named unread field.
_PACKAGING: dict[str, str] = {"T": "taping"}

#: ④ Rated voltage: the shared industry codes plus `1V` = 35 Vdc, which is TDK's
#: and not in the tables Murata prints. It is item (4) of the catalogue's own
#: rated-voltage table and it is *common* — `C1608X5R1V106M080AC` (10 µF 35 V
#: 0603) and `C3216X7R1V225K160AB` (2.2 µF 35 V 1206) are both stocked parts. Its
#: absence was not a safe omission: `voltage_rating` is `higher_ok`, so a part
#: that decodes without one is invisible to *every* voltage-constrained
#: substitution search rather than merely unlabelled.
_VOLTAGE_V: dict[str, str] = {**_eia.DC_VOLTAGE_V, "1V": "35"}

_PATTERN = re.compile(
    r"^c"
    r"(?P<size>\d{4})"
    rf"(?P<temp_char>{_TEMP_CHAR_ALTERNATION})"
    r"(?P<voltage>[0-9][a-z])"
    r"(?P<capacitance>[0-9r]{3})"
    r"(?P<tolerance>[a-z])"
    r"(?P<thickness>\d{3})?"
    r"(?P<tail>[0-9a-z]{0,6})$"
)

#: Thickness is printed in hundredths of a millimetre: `080` -> 0.80 mm.
_THICKNESS_SCALE = 100


def decode(normalized_mpn: str) -> DecodedPart | None:
    """Decode a normalised TDK C-series part number, or `None` if it is not one."""
    match = _PATTERN.match(normalized_mpn)
    if match is None:
        return None

    parameters: dict[str, str] = {"mounting_type": "SMD", "capacitor_technology": "ceramic"}
    extras: dict[str, str] = {"manufacturer": "TDK"}
    unknown: list[str] = []

    package = _CASE_SIZE.get(match["size"])
    if package is None:
        unknown.append("case_size")
    else:
        parameters["package"] = package

    temp_char = match["temp_char"].upper()
    extras["temperature_characteristic"] = temp_char
    if temp_char in _EIA_TEMP_CHAR:
        parameters["dielectric"] = temp_char

    voltage = _VOLTAGE_V.get(match["voltage"].upper())
    if voltage is None:
        unknown.append("rated_voltage")
    else:
        parameters["voltage_rating"] = f"{voltage} V"

    picofarads = _eia.capacitance_pf(match["capacitance"])
    tolerance = _eia.tolerance_of(match["tolerance"], picofarads)
    _eia.apply_capacitance(parameters, extras, unknown, picofarads, tolerance)

    thickness = match["thickness"]
    tail = match["tail"]
    if thickness is not None:
        extras["thickness_mm"] = f"{int(thickness) / _THICKNESS_SCALE:g}"
        # The current form's tail is a packaging style plus TDK's reserved code.
        # Recording it verbatim keeps it visible without asserting a meaning:
        # the styles are reel/pitch variants no parameter here holds, and the
        # reserved half is TDK-internal.
        if tail:
            extras["undecoded_suffix"] = tail.upper()
    elif tail:
        # Older form: ⑦ packaging is one character and §2.1 prints its table; ⑧ is
        # up to four characters of TDK-internal code, explicitly "manufacturing
        # specific and subject to change", so it is kept verbatim and not read.
        described = _PACKAGING.get(tail[0].upper())
        if described is None:
            unknown.append("packaging")
        else:
            extras["packaging"] = described
        if len(tail) > 1:
            extras["undecoded_suffix"] = tail[1:].upper()

    return DecodedPart(family=FAMILY, parameters=parameters, extras=extras, unknown=tuple(unknown))
