"""The resolver's primitives: normalisation, GTIN validation, format gates.

These are the pieces the chain's correctness rests on and the pieces that need no
database, so they get exhaustive treatment here and the integration suite is free
to be about ordering and outcomes.

The two most valuable tests in the file are the ones that look least interesting:
`normalize_code` preserving control bytes, and the ECIA gate refusing a bare short
ID. Both guard against a *silent* misbehaviour — one would quietly change every
alias key, the other would quietly let step 3 eat payloads belonging to steps 1
and 5.
"""

from __future__ import annotations

import pytest

from app.services.scanning import codes, ecia, lcsc

#: A DigiKey-style payload, control characters intact.
ECIA_PAYLOAD = "[)>\x1e06\x1dPLM358N\x1d1P296-1395-1-ND\x1dQ2500\x1d1TLOT4711\x1e\x04"

#: Full-width digits, written as escapes because the glyphs are confusable with
#: ASCII by design — which is exactly the property under test.
FULLWIDTH_DIGITS = "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18"

#: Real, published check digits, one per GTIN length in circulation.
VALID_GTINS = (
    "96385074",  # EAN-8
    "036000291452",  # UPC-A
    "4006381333931",  # EAN-13
    "10036000291459",  # ITF-14
)


# ---------------------------------------------------------------------------
# normalize_code — the alias lookup key
# ---------------------------------------------------------------------------


def test_the_alias_key_ignores_case_and_cosmetic_separators() -> None:
    """Bind time and resolve time must agree; everything else is negotiable.
    A wedge scanner that upper-cases, or a human retyping a hyphenated code off a
    label, has to land on the same key as the scan that taught the binding."""
    assert codes.normalize_code("  LM-358_N.  ") == "lm358n"
    assert codes.normalize_code("lm358n") == codes.normalize_code("LM 358 N")
    # A trailing CRLF is what a HID wedge appends as its terminator.
    assert codes.normalize_code("296-1395-1-ND\r\n") == "29613951nd"


def test_the_alias_key_keeps_the_control_bytes_that_are_the_payload() -> None:
    """**The load-bearing one.** Python treats GS/RS as whitespace
    (`"\\x1d".isspace()` is True), so a `\\s`-based squash would silently eat the
    separators that *are* an ECIA payload's structure — flattening every
    whole-payload alias key and losing the field boundaries with it. The regex is
    written out character by character precisely to avoid that."""
    assert "\x1d".isspace()  # the trap this test exists for
    normalized = codes.normalize_code(ECIA_PAYLOAD)

    assert normalized.count("\x1d") == ECIA_PAYLOAD.count("\x1d")
    assert "\x1e" in normalized
    assert "\x04" in normalized
    # ...while still folding case and dropping the hyphens inside the DI values.
    assert "296-1395" not in normalized
    assert "plm358n" in normalized


def test_a_payload_of_only_separators_normalises_to_nothing() -> None:
    """Which is how the bind endpoint knows to refuse it: a binding keyed on an
    empty string would shadow every payload that normalises away."""
    assert codes.normalize_code("  --__..  ") == ""


# ---------------------------------------------------------------------------
# normalize_mpn — the parts.mpn_norm key
# ---------------------------------------------------------------------------


def test_the_mpn_key_drops_every_kind_of_decoration() -> None:
    """A part number printed with hyphens, slashes or spaces is the same part
    number. This is more aggressive than the alias key on purpose."""
    assert codes.normalize_mpn("ECA-1EM101") == "eca1em101"
    assert codes.normalize_mpn("ECA1EM101") == codes.normalize_mpn("eca 1em/101")
    assert codes.normalize_mpn("296-1395-1-ND") == "29613951nd"


def test_the_mpn_key_of_pure_decoration_is_empty() -> None:
    """So the lookup can skip rather than match every row with a NULL-ish key.
    Non-Latin text lands here too, which is what stops a unicode payload from
    reaching the parts table with a meaningless key."""
    assert codes.normalize_mpn("--- ///") == ""
    assert codes.normalize_mpn("\u65e5\u672c\u8a9e \u2728") == ""


# ---------------------------------------------------------------------------
# short_id_candidate — the tag and QR payload
# ---------------------------------------------------------------------------


def test_the_tag_url_is_unwrapped_to_the_code_inside_it() -> None:
    """`{base_url}/s/{short_id}` is what is physically written to every tag and
    QR, so this is the *normal* case, not an edge one."""
    assert codes.short_id_candidate("https://almagest.lan/s/4K7T-92MQ") == "4K7T-92MQ"
    assert codes.short_id_candidate("HTTPS://ALMAGEST.LAN/S/4K7T92MQ") == "4K7T92MQ"
    assert codes.short_id_candidate("https://almagest.lan/s/4K7T92MQ?utm=nfc") == "4K7T92MQ"
    assert codes.short_id_candidate("https://almagest.lan/s/4K7T92MQ/") == "4K7T92MQ"


def test_the_host_in_a_tag_url_is_deliberately_ignored() -> None:
    """A tag written before a hostname change must keep resolving. Rewriting the
    payload of every tag is the one repair this design cannot make cheaply, so
    the code — not the host — is the payload's authority."""
    assert codes.short_id_candidate("http://192.168.1.9:8000/s/4K7T92MQ") == "4K7T92MQ"
    assert codes.short_id_candidate("https://someone-elses-host/s/4K7T92MQ") == "4K7T92MQ"


def test_a_bare_code_is_its_own_candidate() -> None:
    assert codes.short_id_candidate("  4K7T-92MQ  ") == "4K7T-92MQ"
    assert codes.short_id_candidate("LM358N") == "LM358N"


# ---------------------------------------------------------------------------
# is_gtin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("digits", VALID_GTINS)
def test_published_gtins_validate(digits: str) -> None:
    assert codes.is_gtin(digits)


def test_a_wrong_check_digit_is_not_a_gtin() -> None:
    """The check digit is the whole justification for claiming a payload as a
    retail barcode: an arbitrary run of digits passes only 1 time in 10, and a
    near miss falls through to `unknown` where a human can bind it."""
    assert not codes.is_gtin("4006381333930")
    assert not codes.is_gtin("96385075")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "4006381",  # no GTIN is 7 digits
        "400638133393",  # 12 digits, but not a valid UPC-A either
        "40063813339311",  # 14 digits with the wrong check
        "400638133393X",
        "4006381 333931",  # separators are the caller's job to strip first
    ],
)
def test_non_gtins_are_rejected(value: str) -> None:
    assert not codes.is_gtin(value)


def test_non_ascii_digits_are_not_digits() -> None:
    """`str.isdigit()` is true for these, which is exactly how a unicode payload
    sneaks into a numeric code path. The check is an explicit ASCII class."""
    assert FULLWIDTH_DIGITS.isdigit()
    assert not codes.is_gtin(FULLWIDTH_DIGITS)


# ---------------------------------------------------------------------------
# The ECIA format gate
# ---------------------------------------------------------------------------


def test_a_well_formed_label_is_recognised_and_parsed() -> None:
    label = ecia.parse(ECIA_PAYLOAD)
    assert label is not None
    assert label.customer_part_number == "LM358N"
    assert label.supplier_part_number == "296-1395-1-ND"
    assert label.lot_code == "LOT4711"
    assert ecia.quantity_milli(label) == 2_500_000
    assert label.confidence == pytest.approx(1.0)


def test_the_quantity_becomes_milli_units() -> None:
    """`Q` is a whole piece count and this schema stores thousandths, so the
    conversion is exact and ledger sums stay summable without rounding."""
    label = ecia.parse("[)>\x1e06\x1dQ7\x1dPX\x1e\x04")
    assert label is not None
    assert ecia.quantity_milli(label) == 7000


@pytest.mark.parametrize(
    "payload",
    [
        "4K7T92MQ",  # a short ID — and `4K` is a real DI (purchase order)
        "PARTS123",  # `P` is a real DI (customer part number)
        "SN12345678",  # `S` is a real DI (serial)
        "4006381333931",  # an EAN-13
        "https://almagest.lan/s/4K7T92MQ",
        "LM358N",
        "",
    ],
)
def test_the_ecia_gate_refuses_payloads_that_belong_to_other_steps(payload: str) -> None:
    """**The other load-bearing test.** The library's contract is "degrades, never
    raises", so `parse()` returns a *label* for any input at all — feed it the
    bare short ID `4K7T92MQ` and it reports a purchase order of `7T92MQ`, because
    `4K` is a genuine Data Identifier. That makes its output useless as a format
    test, so the test lives in the adapter and looks for structure: the envelope,
    or at minimum one GS separator. Without this, step 3 would swallow payloads
    that belong to steps 1, 5 and 6."""
    assert not ecia.looks_like_ecia(payload)
    assert ecia.parse(payload) is None


def test_a_cropped_label_with_separators_intact_is_still_claimed() -> None:
    """Missing envelope is a degradation the design says to work through — a
    camera clipped the leading bytes — and one GS is enough structure to be sure
    this is a separated record rather than a word."""
    label = ecia.parse("PLM358N\x1dQ100")
    assert label is not None
    assert label.customer_part_number == "LM358N"
    assert "missing_envelope" in label.warnings
    assert label.confidence < 1.0


def test_a_label_with_an_envelope_but_no_readable_fields_is_not_claimed() -> None:
    """Claiming a payload nothing was extracted from would stop the chain for no
    benefit, so it falls through to the steps after it."""
    assert ecia.parse("[)>\x1e06\x1d\x1e\x04") is None


def test_both_part_number_dis_are_offered_and_deduplicated() -> None:
    """Distributors disagree about which DI carries the manufacturer's part
    number and no marker on the label says which, so both are tried. Guessing one
    would silently fail for half the suppliers."""
    label = ecia.parse("[)>\x1e06\x1dPLM358N\x1d1P296-1395-1-ND\x1e\x04")
    assert label is not None
    assert ecia.mpn_candidates(label) == ("LM358N", "296-1395-1-ND")

    same = ecia.parse("[)>\x1e06\x1dPLM358N\x1d1PLM358N\x1e\x04")
    assert same is not None
    assert ecia.mpn_candidates(same) == ("LM358N",)


# ---------------------------------------------------------------------------
# The LCSC stub
# ---------------------------------------------------------------------------


def test_the_lcsc_handler_is_declared_unsupported() -> None:
    """A constant rather than an inference from behaviour, so switching this
    handler on is a deliberate, reviewable edit — and so the test below fails at
    the moment the claim becomes real instead of passing silently on an empty
    function."""
    assert lcsc.SUPPORTED is False


@pytest.mark.parametrize(
    "payload",
    [
        "C25804",  # an LCSC catalogue number, the form printed on their bags
        "{'pc':'C25804','pm':'0603WAF4701T5E','qty':'100'}",
        "C25804,0603WAF4701T5E,100,RC0603FR-074K7L",
        "https://www.lcsc.com/product-detail/C25804.html",
        "",
    ],
)
def test_the_lcsc_stub_claims_nothing_whatever_it_is_shown(payload: str) -> None:
    """We have no real LCSC samples, and the design says this handler must be
    reverse-engineered from samples. A parser built on a plausible-looking pattern
    does not fail loudly — it returns a **confidently wrong part identification**,
    which becomes a stock movement against the wrong part in an append-only
    ledger. Returning nothing costs one tap: the payload falls through to
    `unknown` and the user binds it once."""
    assert lcsc.parse(payload) is None
