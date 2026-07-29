"""Yageo CC (ceramic capacitors) and RC (thick-film chip resistors).

Two families, one module, because both are read off Yageo's own "ORDERING
INFORMATION - GLOBAL PART NUMBER" block and share its conventions.

**CC**, from the *Surface-Mount Ceramic Multilayer Capacitors, 6.3 V to 50 V*
product specification (Sep 2020 V.20)::

    CC   XXXX   X    X    X7R   X    BB   XXX
         (1)    (2)  (3)         (4)       (5)

    (1) size, inch based   (2) tolerance   (3) packing style
    then the temperature-characteristic material, written out in full
    (4) rated voltage   then a process/termination code   (5) capacitance

**RC**, from the *RC0603 general purpose chip resistors* product specification
(Jul 2008 V.3)::

    RC0603    X    R    -    XX    XXXX    L
              (1)  (2)  (3)  (4)   (5)     (6)

    (1) tolerance   (2) packaging type   (3) temperature coefficient
    (4) taping reel   (5) resistance value   (6) optional symbol

Two things about RC are worth spelling out, because both look like mistakes:

* **(3) is the hyphen itself.** Yageo's table lists exactly one value for it,
  `–` = "base on spec", so the temperature coefficient is *not encoded* — the
  position is a placeholder. `normalize_mpn` removes the hyphen along with every
  other separator, and nothing is lost by that, because there was nothing there.
* **The power rating is not decoded.** `RC0603` is a 0.1 W part, but that is a row
  in the datasheet keyed by series and size, not a field in the number, and Yageo
  ships higher-power chip resistors in the same sizes under other series codes. A
  decoder that emitted it would be reading the datasheet from memory and calling
  it decoding.

**The size field is checked against a list even though it needs no translation.**
It really is the imperial EIA code written out, so there is nothing to look up —
but four digits passed straight through are four digits *nothing* validates, and
`CC9999KRX7R9BB104` used to yield `package == "9999"` with no field named unread.
The size lists below are therefore a shape check, not a translation: a code Yageo
does not build comes back as an unknown field, which is a review-queue item, where
a garbled or OCR'd size otherwise became a confident package facet.

**CC rated voltages of 200 V and up are letter codes, and are not read.** The
digit codes run 6.3 V to 100 V; above that Yageo switches to letters, and
`_CC_PATTERN` accepts only a digit in that position, so such a number is not
claimed by this family at all — no partial decode, and no wrong one.
"""

from __future__ import annotations

import re
from decimal import Decimal

from . import _eia
from ._result import DecodedPart

CC_FAMILY = "yageo_cc"
RC_FAMILY = "yageo_rc"

_CC_PATTERN = re.compile(
    r"^cc"
    r"(?P<size>\d{4})"
    r"(?P<tolerance>[a-z])"
    r"(?P<packing>[a-z])"
    r"(?P<temp_char>[0-9a-z]{3})"
    r"(?P<voltage>[0-9])"
    r"(?P<process>[a-z]{2})"
    r"(?P<capacitance>[0-9r]{3})$"
)

#: `R`/`K`/`M` sit where the decimal point goes and the trailing zero after them is
#: dropped (`1K2`, never `1K20`), so the value is 1 to 3 digits, a multiplier letter,
#: then up to two more digits. The optional `L` is Yageo's customer-label symbol
#: and says nothing about the component.
_RC_PATTERN = re.compile(
    r"^rc"
    r"(?P<size>\d{4})"
    r"(?P<tolerance>[a-z])"
    r"(?P<packaging>[a-z])"
    r"(?P<reel>\d{2})"
    r"(?P<resistance>\d{1,3}[rkm]\d{0,2})"
    r"(?P<optional>l)?$"
)

#: CC (1) Size, inch based. A shape check, not a translation — see the module
#: docstring. These are the sizes the cited specification's ordering table builds;
#: Yageo's high-voltage CC ranges add larger ones, and until that document is read
#: they belong in `unknown` rather than in a set that would also accept `9999`.
_CC_SIZE: frozenset[str] = frozenset({"0201", "0402", "0603", "0805", "1206", "1210"})

#: RC (1) Size, inch based. Same shape check, different series: the RC ordering
#: information covers 0201 through 2512.
_RC_SIZE: frozenset[str] = frozenset(
    {"0201", "0402", "0603", "0805", "1206", "1210", "2010", "2512"}
)

#: CC (2) Tolerance: the eight codes Yageo's ordering table lists. They are the
#: same industry letters `_eia` already transcribes from Murata's and TDK's
#: catalogues, so the *values* come from there and this set only bounds which
#: letters Yageo actually issues — `W` (±0.05 pF) and `Z` (+80/-20%) are in the
#: shared table and not in Yageo's, so they stay unread here.
#:
#: This was `{"J", "K", "M"}` with a comment claiming the specification listed only
#: those three. It lists eight, and the missing five are not exotic:
#: `CC0603FRNPO9BN100` is a stocked 10 pF ±1% C0G part that decoded with no
#: tolerance at all, i.e. as a point value.
_CC_TOLERANCE_CODES: frozenset[str] = frozenset({"B", "C", "D", "F", "G", "J", "K", "M"})

#: CC (3) Packing style.
_CC_PACKING: dict[str, str] = {
    "R": "paper/PE taping reel, 7 inch",
    "K": "blister taping reel, 7 inch",
    "P": "paper/PE taping reel, 13 inch",
    "F": "blister taping reel, 13 inch",
}

#: CC (4) Rated voltage, in volts DC. `0` = 100 V ends the digit sequence rather
#: than starting a new decade of it, which is why it is a table: `CC0603KRX7R0BB104`
#: is a widely stocked 100 nF **100 V** X7R 0603, and it used to decode with no
#: voltage rating because the table stopped at 50 V with the range of one
#: specification.
_CC_VOLTAGE_V: dict[str, str] = {
    "5": "6.3",
    "6": "10",
    "7": "16",
    "8": "25",
    "9": "50",
    "0": "100",
}

#: The temperature-characteristic material is spelled out on the part number, so
#: the only translation needed is Yageo's `NPO` for what EIA calls `C0G` — the
#: same dielectric under the older name. Emitting `NPO` verbatim would create a
#: second spelling of one facet and split it in search.
_CC_DIELECTRIC_ALIASES: dict[str, str] = {"NPO": "C0G"}
_CC_EIA_DIELECTRICS: frozenset[str] = frozenset({"C0G", "X5R", "X6S", "X7R", "X7S", "X7T", "Y5V"})

#: RC (1) Tolerance. Only these two are in the RC0603 specification's table.
_RC_TOLERANCE_PCT: dict[str, str] = {"F": "1", "J": "5"}

#: RC (2) Packaging type — one documented value.
_RC_PACKAGING: dict[str, str] = {"R": "paper/PE taping reel"}

#: RC (4) Taping reel diameter, in inches.
_RC_REEL: dict[str, str] = {"07": "7", "10": "10", "13": "13"}

#: RC (5) multipliers: `R` marks ohms, `K` kilohms, `M` megohms.
_RC_MULTIPLIER: dict[str, int] = {"R": 0, "K": 3, "M": 6}


def decode_cc(normalized_mpn: str) -> DecodedPart | None:
    """Decode a normalised Yageo CC part number, or `None` if it is not one."""
    match = _CC_PATTERN.match(normalized_mpn)
    if match is None:
        return None

    parameters: dict[str, str] = {"mounting_type": "SMD", "capacitor_technology": "ceramic"}
    extras: dict[str, str] = {"manufacturer": "Yageo"}
    unknown: list[str] = []

    # The size field *is* the imperial EIA code, so there is no lookup and no
    # chance of the metric/imperial mix-up the other three families guard against
    # — but it is still checked, because unchecked digits validate nothing.
    if match["size"] in _CC_SIZE:
        parameters["package"] = match["size"]
    else:
        unknown.append("size")

    temp_char = match["temp_char"].upper()
    dielectric = _CC_DIELECTRIC_ALIASES.get(temp_char, temp_char)
    extras["temperature_characteristic"] = temp_char
    if dielectric in _CC_EIA_DIELECTRICS:
        parameters["dielectric"] = dielectric
    else:
        unknown.append("temperature_characteristic")

    voltage = _CC_VOLTAGE_V.get(match["voltage"])
    if voltage is None:
        unknown.append("rated_voltage")
    else:
        parameters["voltage_rating"] = f"{voltage} V"

    packing = _CC_PACKING.get(match["packing"].upper())
    if packing is None:
        unknown.append("packing_style")
    else:
        extras["packaging"] = packing

    picofarads = _eia.capacitance_pf(match["capacitance"])
    letter = match["tolerance"].upper()
    # Decoded by the shared helper, restricted to Yageo's own letters. Routing it
    # through `_eia.apply_capacitance` is what puts an absolute tolerance
    # (`B`/`C`/`D`, in picofarads) into `extras` instead of dropping it: the value
    # grammar can only carry a percentage, and this family used to discard the rest.
    tolerance = _eia.tolerance_of(letter, picofarads) if letter in _CC_TOLERANCE_CODES else None
    _eia.apply_capacitance(parameters, extras, unknown, picofarads, tolerance)

    # The two characters before the capacitance are the process and termination
    # codes named in the specification's own prose; the specification prints the
    # template value `BB` and no table, so they are recorded, not read.
    extras["undecoded_suffix"] = match["process"].upper()

    return DecodedPart(
        family=CC_FAMILY, parameters=parameters, extras=extras, unknown=tuple(unknown)
    )


def decode_rc(normalized_mpn: str) -> DecodedPart | None:
    """Decode a normalised Yageo RC part number, or `None` if it is not one."""
    match = _RC_PATTERN.match(normalized_mpn)
    if match is None:
        return None

    parameters: dict[str, str] = {"mounting_type": "SMD"}
    # Thick film is what the RC series *is* — the specification's scope line says
    # so — and the series code is the part of the number that carries it.
    extras: dict[str, str] = {"manufacturer": "Yageo", "construction": "thick film"}
    unknown: list[str] = []

    if match["size"] in _RC_SIZE:
        parameters["package"] = match["size"]
    else:
        unknown.append("size")

    tolerance_pct = _RC_TOLERANCE_PCT.get(match["tolerance"].upper())
    if tolerance_pct is None:
        unknown.append("tolerance")

    resistance = _resistance_ohms(match["resistance"])
    if resistance is None:
        unknown.append("resistance")
    else:
        value = _eia.ohms(resistance)
        parameters["resistance"] = value if tolerance_pct is None else f"{value} ±{tolerance_pct}%"
        if resistance == 0:
            # `0R` is Yageo's jumper code, and a jumper is a real stocked part
            # rather than a degenerate resistor.
            extras["is_jumper"] = "true"

    packaging = _RC_PACKAGING.get(match["packaging"].upper())
    if packaging is None:
        unknown.append("packaging")
    else:
        extras["packaging"] = packaging

    reel = _RC_REEL.get(match["reel"])
    if reel is None:
        unknown.append("taping_reel")
    else:
        extras["reel_diameter_inch"] = reel

    return DecodedPart(
        family=RC_FAMILY, parameters=parameters, extras=extras, unknown=tuple(unknown)
    )


def _resistance_ohms(code: str) -> Decimal | None:
    """`10K` -> 10000, `97R6` -> 97.6, `0R` -> 0. `None` if the code is malformed."""
    body = code.upper()
    for letter, exponent in _RC_MULTIPLIER.items():
        if letter not in body:
            continue
        whole, _, frac = body.partition(letter)
        if not whole:
            return None
        return Decimal(f"{whole}.{frac or '0'}").scaleb(exponent)
    return None
