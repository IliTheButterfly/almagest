"""`_split_snippet` — turning `snippet()`'s marker-delimited string into segments.

Unit-level because the property under test (marker parsing) holds with no
database involved; the FTS5-side behaviour (that `snippet()` actually produces
these markers, and that ranking is correct) is
`tests/integration/test_datasheet_search.py`'s job.
"""

from __future__ import annotations

from app.services.search.datasheets import SnippetSegment, _split_snippet


def _seg(text: str, highlighted: bool = False) -> SnippetSegment:
    return SnippetSegment(text=text, highlighted=highlighted)


def test_a_plain_string_with_no_markers_is_one_unhighlighted_segment() -> None:
    assert _split_snippet("no markers here") == (_seg("no markers here"),)


def test_a_single_highlighted_run_splits_into_three_segments() -> None:
    raw = "before \x01middle\x02 after"
    assert _split_snippet(raw) == (
        _seg("before "),
        _seg("middle", highlighted=True),
        _seg(" after"),
    )


def test_a_highlighted_run_at_the_very_start_has_no_leading_empty_segment() -> None:
    raw = "\x01alpha\x02 rest"
    assert _split_snippet(raw) == (_seg("alpha", highlighted=True), _seg(" rest"))


def test_multiple_highlighted_runs_in_one_snippet() -> None:
    raw = "\x01alpha\x02 middle \x01beta\x02"
    assert _split_snippet(raw) == (
        _seg("alpha", highlighted=True),
        _seg(" middle "),
        _seg("beta", highlighted=True),
    )


def test_an_unclosed_start_marker_degrades_to_unhighlighted_rather_than_crashing() -> None:
    """`snippet()`'s own output is trusted, but the parser must not assume it —
    a missing close marker must under-highlight, never raise."""
    raw = "before \x01unterminated"
    assert _split_snippet(raw) == (_seg("before "), _seg("unterminated"))


def test_the_empty_string_splits_to_nothing() -> None:
    assert _split_snippet("") == ()
