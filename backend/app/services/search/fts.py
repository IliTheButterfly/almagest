"""FTS5 query construction and the `param_digest` that feeds it.

## Why user text is tokenised rather than escaped

`MATCH` takes a **query language**, not a literal string. A bare `"` opens a
phrase, `*` means prefix, `:` filters a column, `^` anchors, parentheses group,
and `AND`/`OR`/`NOT`/`NEAR` are operators. So raw input from a search box either
throws `fts5: syntax error near ...` or — worse, because it is silent — searches
for something the user did not ask for.

The usual reflex is to escape the dangerous characters. This module **allowlists**
instead: it extracts alphanumeric runs and discards everything else. That is not
merely a stricter version of escaping, it is a different guarantee. An escaping
rule has to be complete to be correct, and FTS5's syntax can grow; a token drawn
from `[0-9A-Za-z...]+` cannot contain a quote, so there is nothing left to escape
and nothing for a future syntax addition to reinterpret.

The cost is real and small: a user cannot type FTS5 operators deliberately.
Nobody types `NEAR(a b, 3)` into a parts search, and the parametric filters
already cover the structured querying this would otherwise be for.
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.enums import ValueType
from app.models.parameter import ParameterChoice, ParameterTemplate, ParameterValue

#: Alphanumeric runs, including accented Latin and Greek so that manufacturer
#: names like "Würth" and unit symbols like "Ω" survive to reach the tokenizer.
#: Written as escapes rather than literal range endpoints: a range's boundary
#: characters are chosen for their codepoint, not because anyone means to type
#: that particular glyph, so spelling them out is both clearer and avoids
#: tripping the ambiguous-character lint.
#:
#: Deliberately excludes every FTS5 metacharacter *by construction* rather than
#: by enumeration — see the module docstring on why that is the stronger
#: guarantee.
_TOKEN = re.compile(
    "[0-9A-Za-z"
    "\u00b5"  # MICRO SIGN — "µF" is written this way constantly
    "\u00c0-\u024f"  # Latin-1 Supplement letters, Latin Extended-A and -B
    "\u0370-\u03ff"  # Greek and Coptic — the ohm and mu symbols
    "\u1e00-\u1eff"  # Latin Extended Additional
    "]+"
)

#: A single token longer than this is a scanner misfire or a paste accident, not
#: a search. FTS5 will happily index it; matching it wastes the query.
_MAX_TOKEN = 64

#: More tokens than this cannot be a human's search box either, and each one
#: costs a term lookup.
_MAX_TOKENS = 16


def build_match_query(raw: str, *, prefix_last: bool = True) -> str | None:
    """Turn arbitrary user text into a safe FTS5 `MATCH` expression.

    Returns ``None`` when the input contains no searchable token at all — a
    querystring of pure punctuation is not a free-text term, and the caller
    should skip FTS entirely rather than match nothing.

    Every token is wrapped in double quotes so it is treated as a literal phrase
    even if it happens to spell an operator: a user searching for the word
    ``near`` gets the word, not a proximity query.

    `prefix_last` appends `*` to the final token, which makes the search behave
    as type-ahead — "resis" finds resistors. Only the last token, because
    prefixing every term matches far too much and the earlier words in a query
    are the ones the user has finished typing.
    """
    tokens = [token for token in _TOKEN.findall(raw) if len(token) <= _MAX_TOKEN]
    if not tokens:
        return None
    tokens = tokens[:_MAX_TOKENS]

    # Safe without escaping: `_TOKEN` cannot match a quote, so no token can
    # close the one wrapping it.
    quoted = [f'"{token}"' for token in tokens]
    if prefix_last:
        quoted[-1] = f"{quoted[-1]}*"

    # Space-separated terms are implicitly AND-ed in FTS5, which is what a search
    # box is expected to do: more words should narrow, not widen.
    return " ".join(quoted)


# ---------------------------------------------------------------------------
# param_digest
# ---------------------------------------------------------------------------

_DIGEST_SQL = text(
    """
    UPDATE part_fts
       SET param_digest = :digest
     WHERE rowid = :part_id
    """
)


def build_param_digest(session: Session, part_id: int) -> str:
    """A compact text rendering of a part's parameter values.

    This is what lets free text find "10k 0805" without the user building a
    parametric query. It is a *search* projection, not storage: the authoritative
    values stay in `parameter_value`, and this is regenerated from them.

    Numerics contribute their engineering-notation display form, so "4700" is
    findable as "4.7 kΩ" — matching what is printed on the label and what a user
    would type. Enum facets contribute both the key and the label, so `0603` and
    its metric spelling both hit.
    """
    rows = session.execute(
        text(
            """
            SELECT pv.display_mantissa, pv.display_si_prefix, pv.display_unit_symbol,
                   pv.raw_input, pv.value_text, pt.value_type, pt.display_name,
                   pc.key AS choice_key, pc.label AS choice_label
              FROM parameter_value AS pv
              JOIN parameter_template AS pt ON pt.id = pv.template_id
              LEFT JOIN parameter_choice AS pc ON pc.id = pv.choice_id
             WHERE pv.part_id = :part_id
             ORDER BY pt.sort_order, pt.name
            """
        ),
        {"part_id": part_id},
    ).all()

    parts: list[str] = []
    for row in rows:
        if row.value_type == ValueType.ENUM and row.choice_key:
            parts.extend(filter(None, [row.choice_key, row.choice_label]))
        elif row.value_type == ValueType.NUMERIC and row.display_mantissa is not None:
            mantissa = f"{row.display_mantissa:g}"
            unit = f"{row.display_si_prefix or ''}{row.display_unit_symbol or ''}"
            parts.append(f"{mantissa}{unit}" if unit else mantissa)
            # The raw input too, so someone who filed it as "4k7" can find it by
            # typing "4k7" rather than "4.7 kΩ".
            if row.raw_input:
                parts.append(row.raw_input)
        elif row.value_text:
            parts.append(row.value_text)

    # Order-preserving dedupe: a package key and label often repeat a token.
    return " ".join(dict.fromkeys(part for part in parts if part.strip()))


def refresh_param_digest(session: Session, part_id: int) -> str:
    """Recompute and store one part's digest.

    Called after any `parameter_value` write. Kept out of the database triggers
    on purpose: the digest is derived from `parameter_value`, not from `parts`,
    and a trigger chain that reindexed a part every time one of its dozen
    parameters changed would reindex it a dozen times during an import.
    """
    digest = build_param_digest(session, part_id)
    session.execute(_DIGEST_SQL, {"digest": digest, "part_id": part_id})
    return digest


def rebuild_all_param_digests(session: Session) -> int:
    """Recompute every digest. The escape hatch, like every other cache here.

    Returns the number of parts touched.
    """
    part_ids = session.execute(text("SELECT id FROM parts ORDER BY id")).scalars().all()
    for part_id in part_ids:
        refresh_param_digest(session, part_id)
    return len(part_ids)


def choice_tokens(session: Session, template: ParameterTemplate) -> list[str]:
    """Every spelling of a template's choices, for building a filter UI."""
    rows = session.execute(
        text("SELECT key, label FROM parameter_choice WHERE template_id = :t"),
        {"t": template.id},
    ).all()
    return [token for row in rows for token in (row.key, row.label) if token]


__all__ = [
    "ParameterChoice",
    "ParameterValue",
    "build_match_query",
    "build_param_digest",
    "choice_tokens",
    "rebuild_all_param_digests",
    "refresh_param_digest",
]
