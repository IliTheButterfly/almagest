"""Generic SMD resistor value codes: 3-digit, 4-digit and EIA-96.

These are **markings, not part numbers**. Nobody orders a `4701`; it is what is
printed on top of a 4.7 kΩ chip resistor. That difference is why every result here
carries `is_marking=True`, and why a caller decoding a *part number* must treat a
hit from this family as a review-queue candidate rather than a fact: `104` is
100 kΩ on a resistor and 100 nF on a capacitor, and three digits cannot say which
component you are holding. The manufacturer families never have that problem —
their prefixes are self-identifying.

Registered with the **empty prefix**, so "most specific prefix wins" puts it last
by construction rather than by a special case in the resolver.

Three formats, all fully specified:

* **3-digit** — two significant digits and a count of zeros, `473` = 47 kΩ. Below
  10 Ω an `R` marks the decimal point: `4R7` = 4.7 Ω, `R47` = 0.47 Ω.
* **4-digit** — three significant digits and a count of zeros, `4701` = 4.70 kΩ,
  with the same `R` convention: `47R0` = 47.0 Ω.
* **EIA-96** — a two-digit index into the E96 series and a letter decade
  multiplier, `68C` = 499 × 100 = 49.9 kΩ.

**Tolerance is not decoded.** By convention 3-digit markings appear on 5% parts
and EIA-96 on 1% parts, but convention is not encoding: a 0.5% part can carry an
EIA-96 mark, and nothing in the three characters says otherwise. The E96 series
membership *is* encoded, and is reported, because that is the strongest
error-correction signal the colour-band and OCR readers have.

Nor is the package: a marking says nothing about whether it is on an 0603 or a
2512.

**Nor the mounting type, which used to be emitted here as `SMD`.** It looked free
— these are chip-resistor formats — but nothing in the characters says so. The
identical `103` is printed on a through-hole 3296 trimmer potentiometer, and `104`
on a through-hole ceramic disc capacitor. So the marking yields a *resistance
under the resistor reading* and nothing else; whether the thing in your hand is
surface mount is something the scan's context knows and the three digits do not.
`is_marking=True` says the reading itself is provisional, and it is not a licence
to add facets the marking does not carry.
"""

from __future__ import annotations

import re
from decimal import Decimal

from . import _eia
from ._result import DecodedPart

FAMILY = "smd_resistor_code"

#: The E96 series is defined as 10^(k/96) rounded to three significant figures,
#: and generating it that way rather than typing 96 values removes the only real
#: risk in this module — a transcription slip in the middle of a table nobody
#: reads. `tests/unit/test_mpn_decoders.py` pins all 96 entries against the
#: published series, so the shortcut is checked rather than trusted.
_E96: tuple[int, ...] = tuple(round(100 * 10 ** (index / 96)) for index in range(96))

#: EIA-96 decade multipliers, as decimal exponents applied to the E96 entry as
#: printed (100 to 976), so `A` is x1 and `01A` is 100 ohm. `X`/`S` and `B`/`H` are
#: alternative spellings of one decade; the standard lists `R` as a third spelling
#: of `Y` (x0.01) and it is **deliberately absent**, because a code ending in `R`
#: is refused as ambiguous before it ever reaches this table — see
#: `_from_r_notation`. Listing it would imply a reachable reading that is not.
_EIA96_MULTIPLIER: dict[str, int] = {
    "Z": -3,
    "Y": -2,
    "X": -1,
    "S": -1,
    "A": 0,
    "B": 1,
    "H": 1,
    "C": 2,
    "D": 3,
    "E": 4,
    "F": 5,
}

_THREE_DIGIT = re.compile(r"^\d{3}$")
_FOUR_DIGIT = re.compile(r"^\d{4}$")
_R_NOTATION = re.compile(r"^(?P<whole>\d*)r(?P<frac>\d*)$")
_EIA96 = re.compile(r"^(?P<index>\d{2})(?P<multiplier>[a-z])$")
#: A jumper is marked `0`, `00`, `000` or `0000` depending on how much room the
#: package has.
_JUMPER = re.compile(r"^0{1,4}$")


def decode(normalized_mpn: str) -> DecodedPart | None:
    """Decode a resistor marking, or `None` if the string is not one.

    `None` is the answer for the overwhelming majority of inputs, and has to be:
    this family sees everything no manufacturer prefix claimed, so a loose
    grammar here would turn every unrecognised part number into a resistance.
    """
    ohms = _resistance_ohms(normalized_mpn)
    if ohms is None:
        return None

    extras = {"marking": normalized_mpn.upper()}
    if _EIA96.match(normalized_mpn) is not None:
        # Only EIA-96 indexes a series; the digit formats can carry any value.
        extras["e_series"] = "E96"

    return DecodedPart(
        family=FAMILY,
        parameters={"resistance": _eia.ohms(ohms)},
        extras=extras,
        is_marking=True,
    )


def _resistance_ohms(code: str) -> Decimal | None:
    if _JUMPER.match(code) is not None:
        return Decimal(0)

    r_notation = _R_NOTATION.match(code)
    if r_notation is not None:
        return _from_r_notation(code, r_notation["whole"], r_notation["frac"])

    if _THREE_DIGIT.match(code) is not None:
        return Decimal(code[:2]).scaleb(int(code[2]))
    if _FOUR_DIGIT.match(code) is not None:
        return Decimal(code[:3]).scaleb(int(code[3]))

    eia96 = _EIA96.match(code)
    if eia96 is not None:
        return _from_eia96(int(eia96["index"]), eia96["multiplier"].upper())

    return None


def _from_r_notation(code: str, whole: str, frac: str) -> Decimal | None:
    """`4R7` -> 4.7, `47R0` -> 47.0, `R47` -> 0.47. `None` when ambiguous.

    A three-character code ending in `R` is **refused**, because it has two legal
    readings that disagree: `10R` is 10 Ω read as a 3-digit marking with the
    decimal point at the end, and 1.24 Ω read as EIA-96 (index 10 = 124, `R` = the
    ×0.01 decade). Both systems are in use on the same shelf and the marking
    itself cannot say which one printed it. Refusing costs one review-queue item;
    guessing puts a resistor eight times off into a circuit.
    """
    if len(code) == 3 and not frac:
        return None
    if not whole and not frac:
        return None
    return Decimal(f"{whole or '0'}.{frac or '0'}")


def _from_eia96(index: int, multiplier: str) -> Decimal | None:
    if not 1 <= index <= len(_E96):
        return None
    exponent = _EIA96_MULTIPLIER.get(multiplier)
    if exponent is None:
        return None
    return Decimal(_E96[index - 1]).scaleb(exponent)
