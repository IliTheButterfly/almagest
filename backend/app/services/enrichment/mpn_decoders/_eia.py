"""The three conventions all four MLCC families genuinely share.

Murata, TDK, Samsung and Yageo each publish their own part-number tables, and
most fields are vendor-private lookups that must live in the vendor's own module.
Three are not: the **three-character capacitance code**, the **tolerance letter**
and the **two-character DC voltage code** are the same industry convention in
every one of them, printed identically in Murata's Cat.No.C02E-16 (pp. 4–5) and
in TDK's general MLCC specification (section 2.1). Sharing them is therefore a
statement of fact about the industry, not a guess that the vendors agree.

The voltage table is written out rather than computed even though it *is*
systematic — mantissa letter (A=1.0, C=1.6, E=2.5, H=5.0, J=6.3 …) times a decade
digit, which reproduces all 15 rows of Murata's table exactly. The formula would
also happily decode `9Z`, a code no manufacturer issues, into a confident number.
An explicit table cannot invent a rating that no catalogue lists, and a rating
this system got wrong is a substitution that destroys a board.

Formatting helpers all emit **ASCII unit spellings** (`uF`, `kohm`) rather than
`µF`/`kΩ`. The value grammar accepts both; ASCII keeps these strings safe to
paste into a shell, a CSV or a bug report without an encoding accident.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

#: Every string reaching these helpers has been through `normalize_mpn`, so it is
#: ASCII `[0-9a-z]` only. The explicit class is still spelled out rather than
#: leaning on `str.isdigit()`, which is `True` for superscripts and non-Latin
#: digits — the same trap `services.scanning.codes` documents.
_DIGITS = re.compile(r"^[0-9]+$")

#: Murata Cat.No.C02E-16 p.4 (field 6, Rated Voltage) plus TDK general MLCC
#: specification section 2.1 (3).
#: Values are volts DC. Only rows *both* catalogues print live here; a code one
#: vendor issues and the other does not stays in that vendor's module, because a
#: shared table is read by every family and would answer for a number whose maker
#: never assigned the code — Murata's non-systematic `YA` (35 V) and `YD` (300 V)
#: are in `murata.py`, TDK's `1V` (35 V) in `tdk.py`.
DC_VOLTAGE_V: dict[str, str] = {
    "0E": "2.5",
    "0G": "4",
    "0J": "6.3",
    "1A": "10",
    "1C": "16",
    "1E": "25",
    "1H": "50",
    "2A": "100",
    "2D": "200",
    "2E": "250",
    "2H": "500",
    "2J": "630",
    "3A": "1000",
    "3D": "2000",
    "3F": "3150",
}

#: Percentage tolerance letters. `F` is deliberately absent — see
#: `tolerance_of`: below 10 pF it means ±1 pF, not ±1%, and which one applies
#: depends on the capacitance, so it cannot be a plain table lookup.
_TOLERANCE_PCT: dict[str, str] = {
    "G": "2",
    "J": "5",
    "K": "10",
    "M": "20",
}

#: Absolute tolerance letters, in picofarads. Used on small Class-1 parts where a
#: percentage of a few pF would be meaningless.
_TOLERANCE_PF: dict[str, str] = {
    "W": "0.05",
    "B": "0.1",
    "C": "0.25",
    "D": "0.5",
}

#: **Strictly below** this, `F` means ±1 pF; at it and above, ±1%. Samsung's MLCC
#: catalogue prints the boundary as a pair of inequalities under its tolerance
#: table — "For Values < 10 pF, F = ±1 pF / Values >= 10 pF, F = ±1%" — so 10 pF
#: itself is a percentage part. (TDK and Murata tabulate `F` as ±1% with no
#: conditional at all, which is the same reading at and above 10 pF.)
#:
#: The boundary lands exactly on 10 pF, an E-series value, so an off-by-one here
#: is not a rounding curiosity: it mis-tolerances every stocked 10 pF `F` part by
#: a factor of ten, in the widening direction, on precisely the Class-1 parts
#: bought *for* their tolerance.
_ABSOLUTE_F_LIMIT_PF = Decimal(10)


@dataclass(frozen=True)
class Tolerance:
    """A tolerance in whichever form the letter actually means.

    Only `pct` can be folded into a value string, because the value grammar's
    tolerance production is `±<digits>%` and nothing else. The other two forms
    are reported as text so they are not quietly dropped, and are **not**
    converted into a percentage: dividing ±0.25 pF by the nominal would fabricate
    a figure the manufacturer never printed.
    """

    #: Symmetric percentage, ready to append to a value string.
    pct: str | None = None
    #: Symmetric absolute tolerance, in picofarads.
    abs_pf: str | None = None
    #: Anything the grammar cannot express, e.g. `+80%/-20%`.
    text: str | None = None


def tolerance_of(code: str, capacitance_pf: Decimal | None) -> Tolerance | None:
    """Decode a capacitance-tolerance letter. `None` means "not in any table".

    `capacitance_pf` is needed only to resolve `F`. Passing `None` for it when
    the capacitance itself failed to decode is correct and returns `None` for
    `F` — guessing which of ±1 pF and ±1% was meant is exactly the kind of
    plausible-looking error this module exists to avoid.
    """
    letter = code.upper()
    if letter in _TOLERANCE_PCT:
        return Tolerance(pct=_TOLERANCE_PCT[letter])
    if letter in _TOLERANCE_PF:
        return Tolerance(abs_pf=_TOLERANCE_PF[letter])
    if letter == "Z":
        # Asymmetric, and the grammar has no form for it. Y5V parts only.
        return Tolerance(text="+80%/-20%")
    if letter == "F":
        if capacitance_pf is None:
            return None
        if capacitance_pf < _ABSOLUTE_F_LIMIT_PF:
            return Tolerance(abs_pf="1")
        return Tolerance(pct="1")
    return None


def capacitance_pf(code: str) -> Decimal | None:
    """Decode a three-character capacitance code to picofarads.

    Two significant digits plus a count of trailing zeros (`104` = 100 000 pF),
    with `R` standing in for a decimal point (`R50` = 0.5 pF, `1R0` = 1.0 pF), in
    which case every character is significant. Identical in all four families'
    catalogues.
    """
    body = code.upper()
    if len(body) != 3:
        return None

    if "R" in body:
        if body.count("R") != 1:
            return None
        whole, _, frac = body.partition("R")
        # `R50` and `50R` both leave one side empty; that is legal, `RR5` is not.
        if not _DIGITS.match(whole + frac):
            return None
        return Decimal(f"{whole or '0'}.{frac or '0'}")

    if not _DIGITS.match(body):
        return None
    return Decimal(body[:2]).scaleb(int(body[2]))


def farads(picofarads: Decimal) -> str:
    """`Decimal(100000)` -> `"100 nF"`. Engineering notation, ASCII unit."""
    for exponent, prefix in ((12, "F"), (9, "mF"), (6, "uF"), (3, "nF")):
        if picofarads >= Decimal(1).scaleb(exponent):
            return f"{_plain(picofarads.scaleb(-exponent))} {prefix}"
    return f"{_plain(picofarads)} pF"


def ohms(value: Decimal) -> str:
    """`Decimal(10000)` -> `"10 kohm"`. Engineering notation, ASCII unit."""
    for exponent, prefix in ((9, "Gohm"), (6, "Mohm"), (3, "kohm")):
        if value >= Decimal(1).scaleb(exponent):
            return f"{_plain(value.scaleb(-exponent))} {prefix}"
    return f"{_plain(value)} ohm"


def with_tolerance(value: str, tolerance: Tolerance | None) -> str:
    """Append `±n%` when — and only when — the grammar can carry it."""
    if tolerance is None or tolerance.pct is None:
        return value
    return f"{value} ±{tolerance.pct}%"


def apply_capacitance(
    parameters: dict[str, str],
    extras: dict[str, str],
    unknown: list[str],
    picofarads: Decimal | None,
    tolerance: Tolerance | None,
) -> None:
    """Land a decoded capacitance and its tolerance in the right three places.

    All four families reach this with the same code, the same letter and the same
    rules, so they land it the same way — a family that formatted its own would
    eventually format it differently, and two spellings of one capacitance is two
    rows in `parameter_value` that do not compare equal.
    """
    if picofarads is None:
        unknown.append("capacitance")
    else:
        parameters["capacitance"] = with_tolerance(farads(picofarads), tolerance)

    if tolerance is None:
        unknown.append("capacitance_tolerance")
    elif tolerance.abs_pf is not None:
        extras["capacitance_tolerance"] = f"±{tolerance.abs_pf} pF"
    elif tolerance.text is not None:
        extras["capacitance_tolerance"] = tolerance.text


def _plain(value: Decimal) -> str:
    """Shortest exact rendering with no exponent: `1E+2` -> `100`, `4.70` -> `4.7`."""
    return format(value.normalize(), "f")
