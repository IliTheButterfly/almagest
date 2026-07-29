"""Ground truth for the MPN decoders.

Every part number below is a real, orderable part, and every expectation was read
off the manufacturer's own numbering table rather than off a distributor's summary
line — the sources are named beside each row. **These rows are the specification.**
There is no reference decoder to diff against, exactly as with
`tests/fixtures/ecia/*.expected.json`, so a disagreement between this file and the
code is resolved by re-reading the catalogue, never by adjusting the expectation
until it passes.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from elec_value_parser import parse as parse_value

from app.services.enrichment.mpn_decoders import (
    REGISTRY,
    DecodedPart,
    _eia,
    decode,
    murata,
    samsung,
    smd_resistor,
    tdk,
    yageo,
)
from app.services.enrichment.mpn_decoders.smd_resistor import _E96

#: `parameter_template.base_unit` for each numeric template a decoder can emit.
#: Used to prove the emitted strings are consumable by `services.parameters`.
_BASE_UNITS: dict[str, str] = {
    "capacitance": "farad",
    "resistance": "ohm",
    "voltage_rating": "volt",
}


class Case:
    """One hand-verified part number and what it must decode to."""

    def __init__(
        self,
        mpn: str,
        family: str,
        parameters: dict[str, str],
        extras: dict[str, str],
        unknown: tuple[str, ...] = (),
        is_marking: bool = False,
    ) -> None:
        self.mpn = mpn
        self.family = family
        self.parameters = parameters
        self.extras = extras
        self.unknown = unknown
        self.is_marking = is_marking

    def __repr__(self) -> str:
        return self.mpn


CASES: tuple[Case, ...] = (
    # ---------------------------------------------------------------- Murata
    # Murata Cat.No.C02E-16 pp.2-5: 18 = 1.6x0.8mm (0603), 8 = 0.8mm thick,
    # R7 = X7R (EIA), 1H = DC50V, 104 = 10x10^4 pF, K = +-10%, A93 = individual
    # specification code (never decoded), D = 180mm reel / paper taping.
    Case(
        mpn="GRM188R71H104KA93D",
        family="murata_grm",
        parameters={
            "package": "0603",
            "dielectric": "X7R",
            "voltage_rating": "50 V",
            "capacitance": "100 nF ±10%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Murata",
            "thickness_mm": "0.8",
            "temperature_characteristic": "X7R (EIA)",
            "packaging": "180 mm reel, paper taping",
        },
    ),
    # Same catalogue: 5C = C0G (EIA), 101 = 10x10^1 pF = 100 pF, J = +-5%.
    # A Class-1 part, so it exercises the other half of the dielectric table.
    Case(
        mpn="GRM1885C1H101JA01D",
        family="murata_grm",
        parameters={
            "package": "0603",
            "dielectric": "C0G",
            "voltage_rating": "50 V",
            "capacitance": "100 pF ±5%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Murata",
            "thickness_mm": "0.8",
            "temperature_characteristic": "C0G (EIA)",
            "packaging": "180 mm reel, paper taping",
        },
    ),
    # Same catalogue: 31 = 3.2x1.6mm (1206), C = 1.6mm thick (a *letter* thickness
    # code, which is why the thickness table is not arithmetic and why a decoder
    # that assumed digits would drop this field), 475 = 47x10^5 pF = 4.7 uF,
    # L = 180mm reel / embossed taping. DigiKey: 4.7 uF +-10% 50V X7R 1206.
    Case(
        mpn="GRM31CR71H475KA12L",
        family="murata_grm",
        parameters={
            "package": "1206",
            "dielectric": "X7R",
            "voltage_rating": "50 V",
            "capacitance": "4.7 uF ±10%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Murata",
            "thickness_mm": "1.6",
            "temperature_characteristic": "X7R (EIA)",
            "packaging": "180 mm reel, embossed taping",
        },
    ),
    # --------------------------------------------------------------- Samsung
    # Samsung's own specification sheet for this part decodes it field by field:
    # 10 = 0603 inch code, B = X7R, 104 = 100 nF, K = +-10%, B = 50V,
    # 8 = 0.8mm, NNN = electrode/product/special, C = cardboard tape 7" reel.
    Case(
        mpn="CL10B104KB8NNNC",
        family="samsung_cl",
        parameters={
            "package": "0603",
            "dielectric": "X7R",
            "voltage_rating": "50 V",
            "capacitance": "100 nF ±10%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Samsung Electro-Mechanics",
            "temperature_characteristic": "X7R",
            "thickness_mm": "0.8",
            "packaging": "cardboard tape, 7 inch reel",
        },
    ),
    # Samsung's specification sheet for CL05C6R8CB5NNNC: 05 = 0402, C = C0G,
    # 6R8 = 6.8 pF, C = +-0.25 pF (absolute, not a percentage), B = 50V,
    # 5 = 0.5mm. The absolute tolerance stays out of the value string because the
    # value grammar has no form for it, and converting it to a percentage would
    # invent a figure Samsung never printed.
    Case(
        mpn="CL05C6R8CB5NNNC",
        family="samsung_cl",
        parameters={
            "package": "0402",
            "dielectric": "C0G",
            "voltage_rating": "50 V",
            "capacitance": "6.8 pF",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Samsung Electro-Mechanics",
            "temperature_characteristic": "C0G",
            "thickness_mm": "0.5",
            "capacitance_tolerance": "±0.25 pF",
            "packaging": "cardboard tape, 7 inch reel",
        },
    ),
    # ----------------------------------------------------------------- Yageo
    # Yageo "Surface-Mount Ceramic Multilayer Capacitors 6.3V to 50V" Sep 2020
    # V.20: 0603 inch size, K = +-10%, R = paper/PE taping reel 7", X7R written
    # out in full, 9 = 50V, BB = process/termination code (no published table),
    # 104 = 100 nF.
    Case(
        mpn="CC0603KRX7R9BB104",
        family="yageo_cc",
        parameters={
            "package": "0603",
            "dielectric": "X7R",
            "voltage_rating": "50 V",
            "capacitance": "100 nF ±10%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Yageo",
            "temperature_characteristic": "X7R",
            "packaging": "paper/PE taping reel, 7 inch",
            "undecoded_suffix": "BB",
        },
    ),
    # Same specification, NPO spelling: Yageo's NPO is EIA's C0G, and it is
    # normalised so one dielectric does not end up as two facet values in search.
    # 9 = 50V, 180 = 18x10^0 pF, J = +-5%. RS/DigiKey: 18 pF 50V +-5% C0G 0603.
    Case(
        mpn="CC0603JRNPO9BN180",
        family="yageo_cc",
        parameters={
            "package": "0603",
            "dielectric": "C0G",
            "voltage_rating": "50 V",
            "capacitance": "18 pF ±5%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Yageo",
            "temperature_characteristic": "NPO",
            "packaging": "paper/PE taping reel, 7 inch",
            "undecoded_suffix": "BN",
        },
    ),
    # Same specification, exercising the lower end of the voltage table:
    # 6 = 10V, M = +-20%, 105 = 10x10^5 pF = 1 uF. Yageo: 1 uF 10V X5R 0402.
    Case(
        mpn="CC0402MRX5R6BB105",
        family="yageo_cc",
        parameters={
            "package": "0402",
            "dielectric": "X5R",
            "voltage_rating": "10 V",
            "capacitance": "1 uF ±20%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "Yageo",
            "temperature_characteristic": "X5R",
            "packaging": "paper/PE taping reel, 7 inch",
            "undecoded_suffix": "BB",
        },
    ),
    # Yageo "RC0603 general purpose chip resistors" Jul 2008 V.3: F = +-1%,
    # R = paper/PE taping reel, the hyphen is the (undecodable) TCR field,
    # 07 = 7 inch reel, 10K = 10 kohm, L = optional customer-label symbol.
    # Power rating is absent on purpose: 0.1 W is a datasheet row keyed by series
    # and size, not a field of the number.
    Case(
        mpn="RC0603FR-0710KL",
        family="yageo_rc",
        parameters={
            "package": "0603",
            "resistance": "10 kohm ±1%",
            "mounting_type": "SMD",
        },
        extras={
            "manufacturer": "Yageo",
            "construction": "thick film",
            "packaging": "paper/PE taping reel",
            "reel_diameter_inch": "7",
        },
    ),
    # Same specification's resistance rule table, which lists "97R6 = 97.6 ohm"
    # verbatim — the case where R is a decimal point rather than a trailing
    # marker. Yageo: 97.6 ohm 1% 1/8W 0805.
    Case(
        mpn="RC0805FR-0797R6L",
        family="yageo_rc",
        parameters={
            "package": "0805",
            "resistance": "97.6 ohm ±1%",
            "mounting_type": "SMD",
        },
        extras={
            "manufacturer": "Yageo",
            "construction": "thick film",
            "packaging": "paper/PE taping reel",
            "reel_diameter_inch": "7",
        },
    ),
    # ------------------------------------------------------------------- TDK
    # TDK general MLCC specification section 2.1 plus the current form's
    # thickness field: 1608 = C1608 = 0603 inch, X7R written out, 1H = 50V,
    # 104 = 100 nF, K = +-10%, 080 = 0.80mm. AA is packaging plus a reserved
    # code with no published pairing, so it is recorded and not read.
    Case(
        mpn="C1608X7R1H104K080AA",
        family="tdk_c",
        parameters={
            "package": "0603",
            "dielectric": "X7R",
            "voltage_rating": "50 V",
            "capacitance": "100 nF ±10%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "TDK",
            "temperature_characteristic": "X7R",
            "thickness_mm": "0.8",
            "undecoded_suffix": "AA",
        },
    ),
    # Same scheme, and the reason the voltage table is a table: 2J is 630V, not
    # the 63V a "digit then letter" reading might suggest. 3216 = 1206 inch,
    # 182 = 18x10^2 pF = 1.8 nF, J = +-5%, 115 = 1.15mm.
    Case(
        mpn="C3216C0G2J182J115AA",
        family="tdk_c",
        parameters={
            "package": "1206",
            "dielectric": "C0G",
            "voltage_rating": "630 V",
            "capacitance": "1.8 nF ±5%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "TDK",
            "temperature_characteristic": "C0G",
            "thickness_mm": "1.15",
            "undecoded_suffix": "AA",
        },
    ),
    # The older seven-field form from section 2.1's own worked example,
    # C3216 X7R 1C 335 K T: no thickness field, and a single trailing character
    # which section 2.1 *does* tabulate (T = taping). 335 = 33x10^5 pF = 3.3 uF.
    Case(
        mpn="C3216X7R1C335KT",
        family="tdk_c",
        parameters={
            "package": "1206",
            "dielectric": "X7R",
            "voltage_rating": "16 V",
            "capacitance": "3.3 uF ±10%",
            "mounting_type": "SMD",
            "capacitor_technology": "ceramic",
        },
        extras={
            "manufacturer": "TDK",
            "temperature_characteristic": "X7R",
            "packaging": "taping",
        },
    ),
    # -------------------------------------------------- generic value markings
    # A marking yields a resistance and *nothing else* — not the package, and not
    # the mounting type, which these rows used to expect as `SMD`. See
    # `test_a_marking_asserts_only_the_value_it_encodes`.
    #
    # 3-digit: two significant digits and a count of zeros.
    Case(
        mpn="473",
        family="smd_resistor_code",
        parameters={"resistance": "47 kohm"},
        extras={"marking": "473"},
        is_marking=True,
    ),
    # 3-digit with R as the decimal point, for values under 10 ohm.
    Case(
        mpn="4R7",
        family="smd_resistor_code",
        parameters={"resistance": "4.7 ohm"},
        extras={"marking": "4R7"},
        is_marking=True,
    ),
    # 4-digit: three significant digits and a count of zeros.
    Case(
        mpn="4701",
        family="smd_resistor_code",
        parameters={"resistance": "4.7 kohm"},
        extras={"marking": "4701"},
        is_marking=True,
    ),
    # EIA-96: index 68 of E96 is 499, C is the x100 decade.
    Case(
        mpn="68C",
        family="smd_resistor_code",
        parameters={"resistance": "49.9 kohm"},
        extras={"marking": "68C", "e_series": "E96"},
        is_marking=True,
    ),
    # A jumper is a real stocked part, and 0 ohm is a value the ohm quantity
    # explicitly allows.
    Case(
        mpn="000",
        family="smd_resistor_code",
        parameters={"resistance": "0 ohm"},
        extras={"marking": "000"},
        is_marking=True,
    ),
)


def _decoded(case: Case) -> DecodedPart:
    result = decode(case.mpn)
    assert result is not None, f"{case.mpn} was not claimed by any family"
    return result


def _case(mpn: str) -> Case:
    """Look a row up by part number rather than by index — inserting a case above
    should never silently repoint a test at a different part."""
    return next(case for case in CASES if case.mpn == mpn)


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_decodes_to_verified_ground_truth(case: Case) -> None:
    result = _decoded(case)
    assert result.family == case.family
    assert result.parameters == case.parameters
    assert result.extras == case.extras
    assert result.unknown == case.unknown
    assert result.is_marking == case.is_marking


@pytest.mark.parametrize("case", CASES, ids=repr)
def test_numeric_values_are_consumable_by_parameters(case: Case) -> None:
    """Every numeric string must parse, and must yield a bounded interval.

    This is the check that keeps the decoders honest about units. A decoder that
    emitted `"100"` for a capacitance, or `"10k"` with the unit only implied by
    the key, would store a row whose `value_min`/`value_max` are null or wrong —
    and a null-bounded row is invisible to every range query, silently.
    """
    result = _decoded(case)
    numeric = {name: raw for name, raw in result.parameters.items() if name in _BASE_UNITS}
    assert numeric, f"{case.mpn} decoded no numeric parameter at all"

    for name, raw in numeric.items():
        parsed = parse_value(raw, _BASE_UNITS[name])
        low, high = parsed.to_interval()
        assert low is not None and high is not None
        assert low <= high


def test_tolerance_widens_the_stored_interval() -> None:
    """The tolerance is folded into the value string on purpose, not decoratively.

    A 100 nF ±10% part is not the single point 100 nF, and search is an
    interval-overlap test, so the ±10% has to reach `value_min`/`value_max` or a
    query for 90 nF will not find a part that is 90 nF at the edge of tolerance.
    """
    result = _decoded(_case("GRM188R71H104KA93D"))
    parsed = parse_value(result.parameters["capacitance"], "farad")
    low, high = parsed.to_interval()

    assert parsed.tolerance_pct == 10.0
    assert low == pytest.approx(90e-9)
    assert high == pytest.approx(110e-9)


def test_unknown_part_number_is_claimed_by_nobody() -> None:
    """An op-amp is not a passive and must not be decoded as one.

    Worth asserting because the marking family's prefix is empty and therefore
    matches every input: its *grammar* is what refuses this, and a looser grammar
    would turn arbitrary part numbers into resistances.
    """
    assert decode("LM358N") is None
    assert decode("") is None


def test_registry_is_ordered_most_specific_prefix_first() -> None:
    lengths = [len(family.prefix) for family in REGISTRY]
    assert lengths == sorted(lengths, reverse=True)
    # The marking family must be last, since it claims anything.
    assert REGISTRY[-1].prefix == ""
    assert REGISTRY[-1].name == smd_resistor.FAMILY


def test_longer_prefix_wins_over_a_shorter_one_that_also_matches() -> None:
    """`CL…` is Samsung's, never TDK's, though TDK's prefix is `c`."""
    # The collision is real, not hypothetical: assert it before relying on it.
    assert samsung.decode.__module__.endswith("samsung")
    assert "cl".startswith("c")

    prefixes = [family.prefix for family in REGISTRY]
    assert prefixes.index("cl") < prefixes.index("c")

    result = _decoded(_case("CL10B104KB8NNNC"))
    assert result.family == samsung.FAMILY
    assert result.family != tdk.FAMILY
    # And the shorter family genuinely cannot read it, which is why falling
    # through to it would be a bug rather than a harmless retry.
    assert tdk.decode("cl10b104kb8nnnc") is None


@pytest.mark.parametrize(
    "written",
    [
        "RC0603FR-0710KL",
        "rc0603fr-0710kl",
        "RC0603FR0710KL",
        "  rc0603fr 0710kl  ",
        "RC0603FR/0710KL",
    ],
)
def test_case_and_separators_are_normalised_away(written: str) -> None:
    """One part number, five spellings, one answer.

    All of them go through the shared `normalize_mpn`. A second normaliser here
    would eventually disagree with `parts.mpn_norm` and the decoded row would be
    invisible to the resolver while looking perfectly correct in the table.
    """
    result = decode(written)
    assert result is not None
    assert result.parameters["resistance"] == "10 kohm ±1%"


def test_partial_decode_keeps_what_it_read_and_names_what_it_did_not() -> None:
    """A real part whose dielectric and thickness codes are not established.

    CL21Y106KABVPNE is 10 uF +-10% 25V 0805. The `Y` dielectric and `B` thickness
    codes are not in any Samsung document obtained for this work, so they are
    absent rather than guessed — and the fields are named, so the review queue can
    say *what* is missing.
    """
    result = decode("CL21Y106KABVPNE")
    assert result is not None
    assert result.parameters == {
        "package": "0805",
        "voltage_rating": "25 V",
        "capacitance": "10 uF ±10%",
        "mounting_type": "SMD",
        "capacitor_technology": "ceramic",
    }
    assert "dielectric" not in result.parameters
    assert result.unknown == ("dielectric", "thickness", "packaging")


def test_unrecognised_field_code_does_not_shift_the_other_fields() -> None:
    """A GRM number with a voltage code no catalogue lists.

    Everything at a fixed offset is still read; only the voltage is dropped. The
    dangerous failure would be re-reading the number one character over and
    returning a different, entirely plausible capacitor.
    """
    result = decode("GRM188R7ZZ104KA93D")
    assert result is not None
    assert result.unknown == ("rated_voltage",)
    assert "voltage_rating" not in result.parameters
    assert result.parameters["capacitance"] == "100 nF ±10%"
    assert result.parameters["dielectric"] == "X7R"


@pytest.mark.parametrize(
    ("mpn", "field", "missing_parameter", "still_decoded"),
    [
        # Samsung `L` is reported by third-party tables as 35V and is excluded from
        # the table on purpose, so it must come back unknown rather than as the
        # commonest value.
        ("CL10B104KL8NNNC", "rated_voltage", "voltage_rating", "capacitance"),
        # A Yageo CC voltage digit no rated-voltage table assigns. It is `1` and
        # not `0`: `0` is 100V and `CC0603KRX7R0BB104` is a stocked 100V part, so
        # using *it* here asserted the absence of a row that exists — which is how
        # a real part came to lose its voltage rating. See
        # `test_yageo_cc_100v_code_is_read`.
        ("CC0603KRX7R1BB104", "rated_voltage", "voltage_rating", "capacitance"),
        # A Yageo RC tolerance letter the RC0603 specification does not list: the
        # resistance is still decoded, without a tolerance band it cannot support.
        ("RC0603ZR-0710KL", "tolerance", None, "resistance"),
        # A TDK voltage code outside the shared industry table.
        ("C1608X7R9Z104K080AA", "rated_voltage", "voltage_rating", "capacitance"),
    ],
)
def test_a_code_outside_the_table_is_omitted_and_never_defaulted(
    mpn: str, field: str, missing_parameter: str | None, still_decoded: str
) -> None:
    """The one behaviour every table in this package exists to guarantee.

    These part numbers are **constructed**, not catalogued: the point is a code that
    is deliberately *not* in a table, and no orderable part can demonstrate the
    absence of a table row. Substituting a default here — the commonest voltage, the
    commonest tolerance — is the failure this whole design is arranged to prevent,
    because the result looks entirely correct in the row and is wrong in a circuit.

    Constructing them has its own trap, which this test fell into: a code picked as
    "not in the table" has to be absent from the *manufacturer's* table too, not
    merely from ours, or the row silently pins a gap in our transcription as though
    it were intended behaviour.
    """
    result = decode(mpn)
    assert result is not None
    assert field in result.unknown
    if missing_parameter is not None:
        assert missing_parameter not in result.parameters
    # The rest of the number is still read: an unknown code drops one field, it does
    # not abandon the decode.
    assert still_decoded in result.parameters


def test_an_unlistable_tolerance_is_dropped_from_the_value_string() -> None:
    """A resistance with no tolerance band is a point value, not a guessed band."""
    result = decode("RC0603ZR-0710KL")
    assert result is not None
    assert result.parameters["resistance"] == "10 kohm"


def test_malformed_body_under_a_known_prefix_decodes_to_nothing() -> None:
    """A GRM prefix with a body that is not a GRM body.

    `1Z4` is not a capacitance code in any of the four schemes, so the field
    layout does not hold and nothing can be trusted to be where it looks. The
    answer is `None`, and specifically not a fall-through to TDK's `c` prefix
    trying Murata's characters against TDK's field widths.
    """
    assert decode("GRM188R71H1Z4KA93D") is None
    assert decode("CL10") is None
    assert decode("CC0603") is None


def test_ambiguous_three_character_r_marking_is_refused() -> None:
    """`10R` has two legal readings that disagree, so it gets neither.

    10 ohm as a 3-digit marking with a trailing decimal point, 1.24 ohm as EIA-96
    (index 10 = 124, R = the x0.01 decade). Both systems are printed on parts in
    the same drawer. A refusal is one review-queue item; a guess is a resistor
    eight times off in a circuit.
    """
    assert decode("10R") is None
    # Refused uniformly, including the one index where the two readings happen to
    # agree (`01R` is 1 ohm either way). A rule that made an exception there would
    # be arithmetic nobody could review, for one code out of ninety-six.
    assert decode("01R") is None
    # The unambiguous neighbours still work, so this is a targeted refusal and not
    # a hole in the R notation.
    assert decode("R47") is not None
    assert decode("4R7") is not None
    assert decode("47R0") is not None


def test_eia96_index_table_matches_the_published_e96_series() -> None:
    """All 96 values, written out, against a table that is generated.

    The generator is `round(100 * 10 ** (k / 96))`, which is the definition of the
    series — but a definition applied with the wrong rounding, or off by one at
    either end, would produce a table that looks entirely reasonable and is wrong
    in the middle. Pinning every entry is the only check worth having.
    """
    published = (
        100,
        102,
        105,
        107,
        110,
        113,
        115,
        118,
        121,
        124,
        127,
        130,
        133,
        137,
        140,
        143,
        147,
        150,
        154,
        158,
        162,
        165,
        169,
        174,
        178,
        182,
        187,
        191,
        196,
        200,
        205,
        210,
        215,
        221,
        226,
        232,
        237,
        243,
        249,
        255,
        261,
        267,
        274,
        280,
        287,
        294,
        301,
        309,
        316,
        324,
        332,
        340,
        348,
        357,
        365,
        374,
        383,
        392,
        402,
        412,
        422,
        432,
        442,
        453,
        464,
        475,
        487,
        499,
        511,
        523,
        536,
        549,
        562,
        576,
        590,
        604,
        619,
        634,
        649,
        665,
        681,
        698,
        715,
        732,
        750,
        768,
        787,
        806,
        825,
        845,
        866,
        887,
        909,
        931,
        953,
        976,
    )
    assert published == _E96


@pytest.mark.parametrize(
    ("marking", "expected_ohms"),
    [
        # Every decade letter of the EIA-96 multiplier table, on index 01 (=100)
        # so the arithmetic is legible, plus the duplicate spellings vendors use.
        ("01Z", "0.1"),
        ("01Y", "1"),
        ("01X", "10"),
        ("01S", "10"),
        ("01A", "100"),
        ("01B", "1000"),
        ("01H", "1000"),
        ("01C", "10000"),
        ("01D", "100000"),
        ("01E", "1000000"),
        ("01F", "10000000"),
        # And the top of the series, to catch an off-by-one at the far end.
        ("96A", "976"),
    ],
)
def test_eia96_decade_multipliers(marking: str, expected_ohms: str) -> None:
    result = decode(marking)
    assert result is not None
    parsed = parse_value(result.parameters["resistance"], "ohm")
    assert parsed.value_nominal == float(Decimal(expected_ohms))


@pytest.mark.parametrize("marking", ["00A", "97A", "01Q", "12345", "R", "4X7"])
def test_marking_grammar_rejects_out_of_range_and_malformed_codes(marking: str) -> None:
    assert decode(marking) is None


def test_murata_individual_specification_code_is_never_decoded() -> None:
    """Positions 9-11 are an internal id. The only thing to produce is invention."""
    murata_case = _decoded(_case("GRM188R71H104KA93D"))
    assert "A93" not in murata_case.extras.values()
    assert "individual_specification" not in murata_case.extras


def test_every_family_is_reachable_through_the_registry() -> None:
    """A family nobody can reach is a family nobody is testing."""
    registered = {family.name for family in REGISTRY}
    assert registered == {
        murata.FAMILY,
        samsung.FAMILY,
        yageo.CC_FAMILY,
        yageo.RC_FAMILY,
        tdk.FAMILY,
        smd_resistor.FAMILY,
    }
    assert {case.family for case in CASES} == registered


# ---------------------------------------------------------------------------
# Regressions from the table-by-table audit against the vendors' own ordering
# documents. Each docstring says what the wrong answer was, because the shape of
# the mistake is the thing worth remembering: every one of them decoded to
# something that looked entirely reasonable in the row.
# ---------------------------------------------------------------------------


def test_f_tolerance_at_exactly_ten_picofarads_is_a_percentage() -> None:
    """The `F` boundary was `<= 10 pF`; the rule is strictly *below* 10 pF.

    `CL10C100FB8NNNC` is a stocked Samsung 10 pF 50 V C0G part specified +-1%, and
    Samsung's catalogue prints the rule as a pair of inequalities: below 10 pF,
    `F` is +-1 pF; at 10 pF and above, +-1%. Off by one comparison, the part
    decoded as +-1 pF — a band ten times too wide — and worse, an absolute
    tolerance cannot ride in the value string, so `capacitance` became the bare
    point `10 pF` and the real band never reached `value_min`/`value_max` at all.

    The boundary lands exactly on an E-series value, which is why this is not a
    rounding curiosity: 10 pF `F` parts are ordinary stock, and tolerance is the
    field a substitution decision turns on for the Class-1 parts bought for it.
    """
    result = decode("CL10C100FB8NNNC")
    assert result is not None
    assert result.parameters["capacitance"] == "10 pF ±1%"
    assert "capacitance_tolerance" not in result.extras

    parsed = parse_value(result.parameters["capacitance"], "farad")
    low, high = parsed.to_interval()
    assert parsed.tolerance_pct == 1.0
    assert low == pytest.approx(9.9e-12)
    assert high == pytest.approx(10.1e-12)


def test_f_tolerance_below_ten_picofarads_is_still_absolute() -> None:
    """The other side of the same boundary: a shift, not a deletion of the rule."""
    result = decode("CL10C9R0FB5NNNC")
    assert result is not None
    assert result.parameters["capacitance"] == "9 pF"
    assert result.extras["capacitance_tolerance"] == "±1 pF"


@pytest.mark.parametrize("mpn", ["C3216X7R1C335KB", "C3216X7R1C335KC"])
def test_tdk_packaging_table_has_exactly_one_row(mpn: str) -> None:
    """`B` = bulk and `C` = cassette were invented, and cited as if published.

    TDK's ⑦ Packaging Style table has one row, `T` = tape and reel, so TDK issues
    no C-series number ending in `B` or `C` and nothing genuine ever reached those
    rows. What did reach them is every mistyped and OCR'd final character, which
    came back as a confident packaging style instead of a named unread field —
    the plausible default the package preamble forbids, made worse by a module
    docstring asserting §2.1 printed it.
    """
    result = decode(mpn)
    assert result is not None
    assert result.unknown == ("packaging",)
    assert "packaging" not in result.extras

    # The documented row still reads, so this is a deletion of two inventions and
    # not a disabling of the field.
    taped = decode("C3216X7R1C335KT")
    assert taped is not None
    assert taped.extras["packaging"] == "taping"
    assert taped.unknown == ()


def test_samsung_packaging_b_is_not_bulk() -> None:
    """`B` -> "bulk" appears in no Samsung packaging table, nor the word anywhere.

    Samsung's PACKAGING CODE table has no `B` row at all. The module's own preamble
    says every entry is there because a Samsung document decodes it or a catalogued
    part pins it; this one was neither, and `CL10B104KB8NNNB` decoded to a
    packaging fact invented out of its last character.
    """
    result = decode("CL10B104KB8NNNB")
    assert result is not None
    assert result.unknown == ("packaging",)
    assert "packaging" not in result.extras

    # `C` is pinned by the two spec sheets the module cites, and still reads.
    pinned = _decoded(_case("CL10B104KB8NNNC"))
    assert pinned.extras["packaging"] == "cardboard tape, 7 inch reel"


def test_a_marking_asserts_only_the_value_it_encodes() -> None:
    """`mounting_type: SMD` was read out of three digits that do not encode it.

    `103` is 10 kohm on a chip resistor — and the identical marking is printed on
    a through-hole 3296 trimmer potentiometer, as `104` is on a through-hole
    ceramic disc capacitor. The module's own docstring concedes it cannot tell
    resistor from capacitor, then asserted the mounting type anyway. `is_marking`
    was the only mitigation, and it protects nothing until the promotion path that
    reads it exists.

    `mounting_type` is an EXACT-match facet, so a wrong one does not merely
    mislabel the part: it removes it from every correctly-filtered search.
    """
    result = decode("103")
    assert result is not None
    assert result.is_marking
    assert result.parameters == {"resistance": "10 kohm"}
    assert "mounting_type" not in result.extras

    # The manufacturer families keep theirs: `GRM`/`CL`/`CC`/`RC` are surface-mount
    # series by definition, and that comes from the series code, not from a value.
    assert _decoded(_case("GRM188R71H104KA93D")).parameters["mounting_type"] == "SMD"


@pytest.mark.parametrize(
    ("mpn", "family"),
    [
        ("CC9999KRX7R9BB104", yageo.CC_FAMILY),
        ("RC9999FR-0710KL", yageo.RC_FAMILY),
    ],
)
def test_yageo_size_outside_the_series_is_not_a_package(mpn: str, family: str) -> None:
    """Four unvalidated digits became a `package` facet with nothing named unread.

    Every other family looks its size up in a transcribed table, so an unknown code
    lands in `unknown`; Yageo passed `\\d{4}` through on the argument that the field
    already *is* the imperial code and needs no translation. True, and beside the
    point: a table's other job is refusing what is not in it. A garbled or OCR'd
    size was indistinguishable from a decoded one, and `9999` is a facet value no
    query can ever match, filed as though the size had been read.
    """
    result = decode(mpn)
    assert result is not None
    assert result.family == family
    assert "package" not in result.parameters
    assert "size" in result.unknown


@pytest.mark.parametrize("mpn", ["CC1210KRX7R9BB105", "RC0201FR-0710KL", "RC2512FR-0710KL"])
def test_yageo_sizes_the_series_does_build_still_decode(mpn: str) -> None:
    """The check is a filter over Yageo's own size list, not a narrowing to 0603."""
    result = decode(mpn)
    assert result is not None
    assert result.parameters["package"] == mpn[2:6]
    assert "size" not in result.unknown


def test_murata_temp_char_with_no_standard_is_not_attributed_to_one() -> None:
    """`1X` (`SL`) was labelled JIS; the catalogue's JIS/EIA column prints `-`.

    Cosmetic today — only an EIA row may populate `dielectric`, so `SL` never
    reached a parametric facet — but this module's entire claim is that its tables
    are transcriptions of a named page, and an invented standard is the one class
    of error no reader downstream can detect. The designation is published and is
    kept; the attribution is dropped rather than replaced with a placeholder,
    because "SL (none)" reads as a decoded fact about a standard.
    """
    result = decode("GRM1881X1H101JA01D")
    assert result is not None
    assert result.extras["temperature_characteristic"] == "SL"
    assert "dielectric" not in result.parameters
    assert "temperature_characteristic" not in result.unknown

    # Rows whose column *does* name a standard still say so.
    assert _decoded(_case("GRM188R71H104KA93D")).extras["temperature_characteristic"] == "X7R (EIA)"


@pytest.mark.parametrize("mpn", ["C1608X5R1V106M080AC", "C3216X7R1V225K160AB"])
def test_tdk_35_volt_code_is_read(mpn: str) -> None:
    """`1V` = 35 Vdc was absent, silently dropping the rating on stocked TDK parts.

    Omitting a field is the safe direction for a value that would otherwise be
    *wrong*; it is not free. `voltage_rating` is `higher_ok`, so a capacitor that
    decodes without one is invisible to every voltage-constrained substitution
    search rather than merely unlabelled — and 272 of the 2898 C-series numbers in
    TDK's own catalogue carry `1V`.
    """
    result = decode(mpn)
    assert result is not None
    assert result.parameters["voltage_rating"] == "35 V"
    assert "rated_voltage" not in result.unknown


def test_the_tdk_35_volt_code_stays_out_of_the_shared_table() -> None:
    """A vendor-specific row in the shared table would answer for other vendors.

    `_eia.DC_VOLTAGE_V` holds only codes both catalogues print, and every family
    reads it. Murata's 35 V code is `YA`, so a Murata number reading `1V` is not a
    35 V part — it is a number whose voltage field is not in Murata's table, and it
    has to come back that way.
    """
    assert "1V" not in _eia.DC_VOLTAGE_V

    grm = decode("GRM188R71V104KA93D")
    assert grm is not None
    assert "rated_voltage" in grm.unknown
    assert "voltage_rating" not in grm.parameters


@pytest.mark.parametrize(
    ("mpn", "capacitance", "absolute_tolerance"),
    [
        # Yageo's ordering table lists eight tolerance codes; this module had three.
        # `CC0603FRNPO9BN100` is a stocked 10 pF +-1% C0G part that decoded as a
        # bare point value with the tolerance named `unknown`.
        ("CC0603FRNPO9BN100", "10 pF ±1%", None),
        ("CC0603GRNPO9BN100", "10 pF ±2%", None),
        # The absolute codes were not merely missing: this family formatted its own
        # capacitance and had nowhere to put a picofarad tolerance, so it would have
        # dropped them. Routing through the shared helper is what lands them.
        ("CC0603BRNPO9BN100", "10 pF", "±0.1 pF"),
        ("CC0603CRNPO9BN100", "10 pF", "±0.25 pF"),
        ("CC0603DRNPO9BN100", "10 pF", "±0.5 pF"),
    ],
)
def test_yageo_cc_reads_the_tolerance_codes_yageo_lists(
    mpn: str, capacitance: str, absolute_tolerance: str | None
) -> None:
    result = decode(mpn)
    assert result is not None
    assert result.parameters["capacitance"] == capacitance
    assert "capacitance_tolerance" not in result.unknown
    assert result.extras.get("capacitance_tolerance") == absolute_tolerance


@pytest.mark.parametrize("mpn", ["CC0603WRNPO9BN100", "CC0603ZRNPO9BN100"])
def test_yageo_cc_ignores_tolerance_letters_yageo_does_not_issue(mpn: str) -> None:
    """`W` (+-0.05 pF) and `Z` (+80/-20%) are in the shared table and not Yageo's.

    The shared helper decodes the industry letters; the per-family set is what
    decides which of them that family actually stamps on a part. Reusing the helper
    without the set would have quietly widened Yageo's tolerance vocabulary to
    Murata's.
    """
    result = decode(mpn)
    assert result is not None
    assert "capacitance_tolerance" in result.unknown
    assert "capacitance_tolerance" not in result.extras


def test_yageo_cc_100v_code_is_read() -> None:
    """Voltage code `0` = 100 V, on a part the suite used as a not-in-any-table case.

    `CC0603KRX7R0BB104` is a widely stocked 100 nF **100 V** X7R 0603. The table
    stopped at 50 V with the range of the one specification cited, and the test row
    that used this number asserted the resulting gap as intended behaviour — which
    is how an incomplete transcription becomes a pinned expectation.
    """
    result = decode("CC0603KRX7R0BB104")
    assert result is not None
    assert result.parameters["voltage_rating"] == "100 V"
    assert result.unknown == ()


@pytest.mark.parametrize(
    ("mpn", "expected_parameters", "expected_extras"),
    [
        # Murata dimensions `21` = 2.0 x 1.25 mm = 0805 (catalogue p.2).
        ("GRM21BR71H104KA01L", {"package": "0805"}, {"thickness_mm": "1.25"}),
        # Murata `YA` = 35 V: one of the two non-systematic rows on p.4, i.e. the
        # rows that cannot be derived and therefore the rows most worth pinning.
        ("GRM31CR6YA106KA12L", {"voltage_rating": "35 V"}, {}),
        # Murata `C7` = X7S (EIA), so it reaches `dielectric` as well as extras.
        ("GRM188C71H104KA01D", {"dielectric": "X7S"}, {"temperature_characteristic": "X7S (EIA)"}),
        # Shared absolute tolerance `B` = +-0.1 pF, used by all four MLCC families.
        ("GRM1885C1H100BA01D", {"capacitance": "10 pF"}, {"capacitance_tolerance": "±0.1 pF"}),
        # Samsung size `31` = 1206 and voltage `C` = 100 V (CL31B105KCHNNNE, 1 uF
        # 100 V 1206 — thickness `H` is deliberately untabled, hence unknown).
        ("CL31B105KCHNNNE", {"package": "1206", "voltage_rating": "100 V"}, {}),
        # Samsung dielectric `A` = X5R and voltage `Q` = 6.3 V (CL10A106MQ8NNNC).
        ("CL10A106MQ8NNNC", {"dielectric": "X5R", "voltage_rating": "6.3 V"}, {}),
        # Samsung voltage `P` = 10 V.
        ("CL10A105KP8NNNC", {"voltage_rating": "10 V"}, {}),
    ],
)
def test_table_rows_no_ground_truth_case_reaches_are_still_pinned(
    mpn: str, expected_parameters: dict[str, str], expected_extras: dict[str, str]
) -> None:
    """Rows that could be corrupted with the whole suite staying green.

    Mutation-testing the tables found seven rows no `CASES` row exercised — and
    they are the rows most likely to need correcting one day, because they are the
    ones the modules themselves flag as coming from a single sheet or from Murata's
    non-systematic column. An unexercised table row is a transcription nobody is
    checking; a later "tidy-up" edit to one would have shipped silently.

    Some of these numbers are constructed to reach a code rather than quoted from a
    catalogue page. That is why each asserts only the field its code decides.
    """
    result = decode(mpn)
    assert result is not None
    for name, value in expected_parameters.items():
        assert result.parameters[name] == value
    for name, value in expected_extras.items():
        assert result.extras[name] == value
