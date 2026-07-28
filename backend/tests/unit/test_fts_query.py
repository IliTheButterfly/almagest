"""FTS5 `MATCH` query construction.

The whole point of this module is that a search box cannot break the query
language, so these tests are mostly hostile input. They are unit tests because
the property being asserted — that no FTS5 metacharacter survives — holds before
any database is involved, and it is easier to be exhaustive here than through
HTTP.
"""

from __future__ import annotations

import pytest

from app.services.search.fts import build_match_query

#: Every FTS5 metacharacter, plus the ones that merely throw.
METACHARACTERS = ['"', "*", ":", "^", "(", ")", "-", "+", "{", "}", "[", "]", ",", "'", "\\"]


def test_a_plain_term_becomes_a_quoted_phrase() -> None:
    assert build_match_query("resistor", prefix_last=False) == '"resistor"'


def test_multiple_terms_are_implicitly_anded() -> None:
    """More words must narrow, not widen — that is what a search box does."""
    assert build_match_query("ceramic capacitor", prefix_last=False) == '"ceramic" "capacitor"'


def test_the_last_term_gets_a_prefix_wildcard_for_type_ahead() -> None:
    assert build_match_query("resis") == '"resis"*'
    # Only the last: prefixing every term matches far too much, and the earlier
    # words are the ones the user has finished typing.
    assert build_match_query("ceramic capac") == '"ceramic" "capac"*'


@pytest.mark.parametrize("char", METACHARACTERS)
def test_no_metacharacter_survives(char: str) -> None:
    """The core safety property. Asserted per-character so a future syntax
    addition to FTS5 cannot quietly slip through a hand-written escape list."""
    built = build_match_query(f"abc{char}def")
    assert built is not None
    assert char not in built.replace('"', "").replace("*", "")


@pytest.mark.parametrize(
    "hostile",
    [
        '" OR 1=1 --',
        "resistor*",
        'mpn:"secret"',
        "^anchor",
        "a NEAR b",
        "(unbalanced",
        "NOT everything",
        "a OR b",
        'unclosed "quote',
        "\\",
        "*",
        "**",
        ":::",
        "-" * 50,
    ],
)
def test_hostile_input_yields_a_safe_expression_or_nothing(hostile: str) -> None:
    built = build_match_query(hostile)
    if built is None:
        return
    # Balanced quotes, and every `*` is a trailing prefix marker.
    assert built.count('"') % 2 == 0
    assert "**" not in built
    for fragment in built.split():
        assert fragment.startswith('"')
        assert fragment.endswith('"') or fragment.endswith('"*')


def test_operator_words_are_searched_not_interpreted() -> None:
    """A user searching for the word "near" wants the word."""
    assert build_match_query("near", prefix_last=False) == '"near"'
    assert build_match_query("and or not", prefix_last=False) == '"and" "or" "not"'


def test_punctuation_only_input_has_no_searchable_term() -> None:
    """None means "skip FTS", which the caller distinguishes from "match
    nothing" — a querystring of pure punctuation is not a free-text term."""
    for blank in ("", "   ", "!!!", "***", '"""', "-", "()"):
        assert build_match_query(blank) is None


def test_accented_and_greek_characters_survive() -> None:
    """Manufacturer names carry diacritics and units carry Greek letters; the
    tokenizer is configured to fold diacritics, so these must reach it."""
    assert build_match_query("Würth", prefix_last=False) == '"Würth"'
    assert build_match_query("Ω", prefix_last=False) == '"Ω"'
    assert build_match_query("µF", prefix_last=False) is not None


def test_electronics_shorthand_stays_one_token() -> None:
    """`4k7` and `0603` must not be split, or the index cannot find them."""
    assert build_match_query("4k7", prefix_last=False) == '"4k7"'
    assert build_match_query("0603", prefix_last=False) == '"0603"'
    assert build_match_query("10k 0805", prefix_last=False) == '"10k" "0805"'


def test_an_absurd_token_is_dropped() -> None:
    """A 300-character run is a paste accident or a scanner misfire, not a term."""
    assert build_match_query("x" * 300) is None
    # …but it must not poison the rest of the query.
    built = build_match_query(f"resistor {'x' * 300}", prefix_last=False)
    assert built == '"resistor"'


def test_the_term_count_is_bounded() -> None:
    """Each term costs a lookup, and no human types forty words into a search box."""
    built = build_match_query(" ".join(f"w{i}" for i in range(100)), prefix_last=False)
    assert built is not None
    assert len(built.split()) <= 16
