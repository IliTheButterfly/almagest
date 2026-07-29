"""Short-ID checks.

The two properties that matter are exhaustive, not statistical: every single
wrong symbol and every adjacent transposition must be caught. Both are tested
by brute force over the whole space of such errors for a sample of codes,
rather than by spot checks.
"""

from __future__ import annotations

import random

import pytest

from idcodec import shortid
from idcodec.shortid import ALPHABET, DATA_SYMBOLS, InvalidShortId


def _rng(seed: int = 20260727) -> random.Random:
    return random.Random(seed)


def _codes(count: int, seed: int = 20260727) -> list[str]:
    rng = _rng(seed)
    return [shortid.generate(rng.getrandbits) for _ in range(count)]


def test_generated_codes_are_valid() -> None:
    for code in _codes(200):
        assert shortid.validate(code) == code


def test_generated_codes_are_eight_symbols_from_the_alphabet() -> None:
    for code in _codes(100):
        assert len(code) == 8
        assert all(symbol in ALPHABET for symbol in code)


def test_check_symbol_never_falls_outside_the_alphabet() -> None:
    """Crockford would encode residues 32-36 as `*~$=U`, which are font-fragile
    and awkward to type. Rejection sampling keeps every printed code inside the
    32-symbol alphabet."""
    for code in _codes(500):
        assert code[DATA_SYMBOLS] in ALPHABET


def test_every_single_symbol_substitution_is_detected() -> None:
    """Exhaustive over all 8 positions x 31 wrong symbols, for many codes."""
    for code in _codes(40):
        for position in range(len(code)):
            for replacement in ALPHABET:
                if replacement == code[position]:
                    continue
                corrupted = code[:position] + replacement + code[position + 1 :]
                assert not shortid.is_valid(corrupted), f"{code} -> {corrupted} slipped through"


def test_every_adjacent_transposition_is_detected() -> None:
    """The other mistake humans make reading a code off a label."""
    for code in _codes(200):
        for position in range(len(code) - 1):
            if code[position] == code[position + 1]:
                continue  # swapping equal symbols is not an error
            swapped = code[:position] + code[position + 1] + code[position] + code[position + 2 :]
            assert not shortid.is_valid(swapped), f"{code} -> {swapped} slipped through"


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("4K7T-92M8", "4K7T92M8"),
        ("4k7t92m8", "4K7T92M8"),
        ("4K7T 92M8", "4K7T92M8"),
        ("  4K7T-92M8  ", "4K7T92M8"),
        ("4K7T_92M8", "4K7T92M8"),
    ],
)
def test_normalisation_of_cosmetic_variation(typed: str, expected: str) -> None:
    assert shortid.normalize(typed) == expected


def test_confusable_glyphs_are_folded() -> None:
    """O/0 and I/L/1 are the confusions that survive a printed label."""
    assert shortid.normalize("OIL45678") == "011" + "45678"


def test_u_is_not_remapped() -> None:
    """`U` is excluded from the alphabet outright, not merged into another
    symbol — so a code containing one is wrong, not silently reinterpreted."""
    with pytest.raises(InvalidShortId) as excinfo:
        shortid.normalize("4K7TU2MQ")
    assert excinfo.value.reason == "alphabet"


def test_a_display_prefix_is_discarded_not_parsed() -> None:
    """The type prefix is cosmetic. Tolerating it when a human types what they
    saw is not the same as giving it meaning."""
    assert shortid.normalize("BIN 4K7T-92M8") == "4K7T92M8"


@pytest.mark.parametrize(
    ("bad", "reason"),
    [("", "empty"), ("   ", "empty"), ("4K7T", "length"), ("4K7T92M8XX", "length")],
)
def test_malformed_input(bad: str, reason: str) -> None:
    with pytest.raises(InvalidShortId) as excinfo:
        shortid.normalize(bad)
    assert excinfo.value.reason == reason


def test_wrong_check_symbol_is_reported_as_such() -> None:
    code = _codes(1)[0]
    wrong = ALPHABET[(ALPHABET.index(code[-1]) + 1) % len(ALPHABET)]
    with pytest.raises(InvalidShortId) as excinfo:
        shortid.validate(code[:-1] + wrong)
    assert excinfo.value.reason == "check"


def test_the_documented_example_code_is_check_valid() -> None:
    """`4K7T-92M8` is the one code every docstring, docs page and input
    placeholder uses, so it is the code that gets pasted into the entry field
    first. The one originally shipped (`4K7T-92MQ`) failed its own check symbol,
    which the server correctly refused — reading as a bug in the field rather
    than in the example. Pinned here because prose cannot be type-checked.
    """
    assert shortid.is_valid("4K7T92M8")
    assert shortid.normalize("4K7T-92M8") == "4K7T92M8"


def test_display_formatting() -> None:
    assert shortid.format_display("4K7T92M8") == "4K7T-92M8"
    assert shortid.format_display("4K7T92M8", "location") == "BIN 4K7T-92M8"
    assert shortid.format_display("4K7T92M8", "part") == "PART 4K7T-92M8"


def test_display_round_trips_through_normalisation() -> None:
    """Whatever is printed must be typable back in."""
    for code in _codes(50):
        assert shortid.normalize(shortid.format_display(code, "location")) == code


def test_generation_is_reasonably_uniform() -> None:
    """Rejection sampling must not bias the surviving space — a skewed
    generator would raise the collision rate above the 3.6% the design budgets
    for at 5x10^4 objects."""
    codes = _codes(2000, seed=99)
    assert len(set(codes)) == len(codes)

    first_symbols = {code[0] for code in codes}
    assert len(first_symbols) >= len(ALPHABET) - 2
