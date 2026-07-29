"""KiCad BOM import. **The contract is that an import never fails.**

`bom_lines.part_id` is nullable for exactly this reason: a BOM has to land
intact, in one action, even when half its lines name parts the catalogue has
never heard of. This is the same argument as `app.api.routes.intake` — defer the
curation, never block the intake — and it has the same consequence, that every
imported field is kept verbatim beside the resolved one so a better matcher
written next year can be re-run over the original text.

So nothing in `parse_bom` raises on content. A file with no recognisable header,
a row with more cells than columns, an undecodable byte, a quantity of `"four"`
— each becomes a *warning attached to the row it came from* and the row still
lands. The only thing that would justify refusing is input this module cannot
represent at all, and there is none: worst case every cell ends up in
`raw_fields_json` with `qty_per_assembly_milli` defaulted to one, which is a
worklist item rather than a lost file.

**There is no one KiCad BOM format**, so nothing here is keyed to a literal
header row:

* the modern grouped CSV (`Reference`, `Value`, `Footprint`, `Qty`, `DNP`, plus
  whatever symbol fields the user invented) and the old `bom2grouped_csv.xsl`
  output (`Ref`, `Qnty`, `Cmp name`, `Vendor`, behind five rows of preamble) are
  both ordinary input;
* headers are matched through a normalised alias table (case, spacing,
  punctuation and pluralisation all removed), and the alias list per field is
  **ordered**, so a file with both `Footprint` and `Package` uses the former;
* the header row is *found*, not assumed to be row 1, because the old XSL
  templates emit `Source:` / `Date:` / `Component Count:` above it;
* the delimiter is chosen by parsing the whole file with each candidate and
  keeping the one that yields the most recognised columns. Counting separator
  characters — the obvious approach — picks `,` for a semicolon-delimited file
  whose designator cell is `"R1,R2,R3"`, which is most of them.

**Matching is exact-normalised-MPN only.** CLAUDE.md forbids auto-accepting a
part number a machine read, and the same reasoning applies to one a machine
*guessed*: a plausible-but-wrong match silently allocates the wrong component to
a build, which is worse than a line that says "unknown". So a match needs a
unique `parts.mpn_norm` equality hit through `app.services.scanning.codes
.normalize_mpn` — the single definition of that key, because a row written under
a different rule is invisible to the resolver while looking perfectly correct —
and even then `is_match_confirmed` stays false until a human agrees.

The value parser earns its place here as a **refusal**, not an enrichment. Some
BOMs put the part number in `Value` (`U1` = `LM358N`), which is worth matching
on; but so does every passive line (`R7` = `10k`), and `normalize_mpn("10k")` is
a perfectly good lookup key that will happily match somebody's stock part `10K`.
Parsing the value against the quantity its designator prefix implies — `R` is
ohms, `C` farads, `L` henries, and nothing else is attempted, because `U1` = `1M`
is not a resistance and never was — is what tells the two apart. See
`_mpn_candidates`: what makes a cell a part number is the *reason* the parse
failed, not the fact that it failed.

**The designators are not allowed to be the gate's only input**, though, because
they are so often missing or unrecognised: a file with no `Reference` column, a
blank designator cell, an `RN1` resistor network or a `VR1` potentiometer all
imply no quantity, and every one of them used to walk straight past the parser
and match `10k` to a chip resistor named `10K`. So a cell may only be used as a
part number when **no** quantity the parser knows reads it as a value
(`_reads_as_a_quantity`) and it is a single token (`_NOT_ONE_TOKEN`) — which is
also what stops `10k 1%` from being flattened into the key `10k1` and matching
`10K1`, a real 10.1 kΩ part.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from elec_value_parser import ParsedValue, ValueParseError, known_quantities
from elec_value_parser import parse as parse_electronics_value
from sqlalchemy import String, func, select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.projects import BomLine, Project
from app.services.scanning.codes import normalize_mpn


class BomField(StrEnum):
    """A column this schema has a home for.

    Everything else is still imported — it goes to `raw_fields_json` — so adding
    a member here is about gaining a *typed* column, never about accepting data
    that would otherwise be dropped.
    """

    DESIGNATORS = "designators"
    VALUE = "value"
    FOOTPRINT = "footprint"
    QUANTITY = "quantity"
    MPN = "mpn"
    MANUFACTURER = "manufacturer"
    DESCRIPTION = "description"
    DATASHEET = "datasheet"
    DNP = "dnp"


class QtySource(StrEnum):
    """Where a line's `qty_per_assembly_milli` came from.

    Not a stored column — it is the audit trail for the one number in an import
    that is *computed* rather than copied, and the reason a reviewer can tell a
    trustworthy 4 from a reconciled one.
    """

    #: Counted from the expanded designator list. Preferred when they agree,
    #: because it is a statement about the board rather than about the exporter.
    DESIGNATOR_COUNT = "designator_count"
    #: Read from a quantity column, with no designators to check it against.
    DECLARED = "declared"
    #: A quantity column and a designator list that disagree. See `_resolve_qty`.
    DISAGREEMENT_MAX = "disagreement_max"
    #: Neither was usable. One per assembly, so the line still generates demand.
    FALLBACK = "fallback"


#: Alias table, **ordered within each field**, over normalised header text (see
#: `_normalize_header`). Order is the collision rule: a file carrying both
#: `Footprint` and `Package` maps the footprint from the former, and a generic
#: `Part Number` only wins the MPN slot if no column says `MPN` outright.
_ALIASES: tuple[tuple[BomField, tuple[str, ...]], ...] = (
    (
        BomField.DESIGNATORS,
        (
            "reference",
            "references",
            "ref",
            "refdes",
            "referencedesignator",
            "designator",
            "designation",
        ),
    ),
    (BomField.VALUE, ("value", "val", "componentvalue")),
    # `Package` last: it is a real spelling for this column in hand-rolled
    # templates, and also a real name for a *different* column (the 0603 the
    # part comes in) in exports that carry both.
    (BomField.FOOTPRINT, ("footprint", "footprintname", "pcbfootprint", "package")),
    (BomField.QUANTITY, ("quantity", "quantities", "qty", "qnty", "quantityperboard")),
    (
        BomField.MPN,
        (
            "mpn",
            "manufacturerpartnumber",
            "manufacturerpartno",
            "manufacturerpn",
            "mfrpartnumber",
            "mfrpartno",
            "mfgpartnumber",
            "mfgpartno",
            "mfrpn",
            "mfgpn",
            # Ambiguous — could be an internal or a distributor number — so it
            # is accepted only when nothing better is present.
            "partnumber",
            "partno",
        ),
    ),
    (BomField.MANUFACTURER, ("manufacturer", "manufacturername", "mfr", "mfg", "mfrname", "brand")),
    (BomField.DESCRIPTION, ("description", "desc", "descr")),
    (BomField.DATASHEET, ("datasheet", "datasheeturl", "datasheetlink")),
    (BomField.DNP, ("dnp", "donotpopulate", "donotplace", "donotinstall", "dni", "nopopulate")),
)

_ALIAS_LOOKUP: dict[str, tuple[BomField, int]] = {
    alias: (bom_field, priority)
    for bom_field, aliases in _ALIASES
    for priority, alias in enumerate(aliases)
}

#: Everything that is not an ASCII alphanumeric is decoration in a header:
#: `Reference(s)`, `Mfr. Part No`, `Do Not Populate` and `DNP` all have to reach
#: the same key as their neighbours in the next KiCad release.
_NOT_HEADER = re.compile(r"[^0-9a-z]+")

#: Delimiters in circulation. `,` first so it wins a tie — it is what KiCad's
#: own exporters emit, and a tie means the evidence did not distinguish them.
_DELIMITERS = (",", ";", "\t", "|")

#: How far down to look for the header row. `bom2csv.xsl` emits six preamble
#: rows; twenty is slack for a template nobody has seen yet, and small enough
#: that a headerless file is not searched to its end.
_HEADER_SEARCH_ROWS = 20

#: A header row has to map at least this many columns to be believed. One is
#: too few: a preamble row reading `Component Count:,42` maps nothing, but a
#: stray `Value` in a title block would otherwise be taken for the header.
_MIN_HEADER_FIELDS = 2

#: Designator separators. `,` and `;` are what tools write; whitespace is what
#: humans write. `/` is deliberately absent — it is far more often part of a
#: footprint or a value than a separator, and splitting on it would fabricate
#: designators that make the quantity cross-check lie.
_DESIGNATOR_SPLIT = re.compile(r"[,;\s]+")

#: `R1-R5`, `C1..C4`, `D1~D3`, and the dashes a spreadsheet substitutes for the
#: hyphen (U+2010..U+2015 and the minus sign, written as escapes because they are
#: indistinguishable from `-` in a source file). The right-hand prefix is
#: optional, because `R1-5` is written by hand constantly.
_DESIGNATOR_RANGE = re.compile(
    r"^([A-Za-z_]+)(\d+)\s*(?:\.\.|[-\u2010-\u2015\u2212~])\s*([A-Za-z_]*)(\d+)$"
)

#: A range wider than this is a typo (`R1-R99999`), not a board. Expanding it
#: would turn one bad cell into a hundred thousand designators and a demand of
#: 99999 parts; keeping the token verbatim with a warning loses nothing, because
#: the raw text is still on the row.
_MAX_RANGE_SPAN = 512

#: The leading letters of a designator, which is the only thing in a BOM that
#: says what physical quantity the `Value` column is expressing. Deliberately
#: three entries: for anything else the value is a part name, and parsing
#: `LM358` as a number is how a BOM line acquires a fabricated parameter.
_PASSIVE_QUANTITY: Mapping[str, str] = {"R": "ohm", "C": "farad", "L": "henry"}

#: Cells that mean "empty" rather than a value. KiCad writes `~` into an unset
#: `Datasheet` field, and a spreadsheet round-trip turns unset cells into `-`.
_EMPTY_MARKERS = frozenset({"", "~", "-", "n/a", "na"})

#: Cells that mean "not set" in a DNP column. Anything else non-empty is taken
#: as "do not populate", including the literal `DNP` KiCad writes: a flag column
#: whose value nobody recognises is a line the user marked, and reading it as
#: "populate" would put a part on a board that must not have one.
_FALSY = frozenset({"0", "false", "no", "n", "f", "off"})

#: One per assembly, in milli-units, when neither a quantity column nor a
#: designator list produced a number.
_FALLBACK_QTY_MILLI = 1_000

#: **`csv`'s per-field cap, raised once at import.** The stdlib default is 131072
#: characters, which is well *inside* the 5,000,000-character body the API route
#: accepts, so a single wide cell raised `_csv.Error` out of `parse_bom` and out
#: of the endpoint as a bare 500 — breaking this module's never-raises contract
#: on input it advertises as acceptable. The realistic trigger is not a wide cell
#: but one unbalanced quote in a description, which makes the rest of the file
#: one field: a 4000-line export with a single stray `"` was enough.
#:
#: Set at module scope rather than per call because `csv.field_size_limit` is
#: process-global and FastAPI runs handlers in a threadpool — two concurrent
#: imports raising and restoring it would make a large file fail intermittently.
#: A monotonic bump can only make other readers more permissive. Anything past
#: even this bound still degrades to a warning; see `_probe`.
_MAX_FIELD_CHARS = 8_000_000
csv.field_size_limit(_MAX_FIELD_CHARS)


def _column_width(name: str) -> int:
    """The declared width of a `bom_lines` column.

    Read off the model rather than restated, so a truncation here cannot drift
    away from the schema it exists to respect. SQLite does not enforce `VARCHAR`
    lengths, which is exactly why this has to be done in code: without it the
    file imports fine today and fails the day the store is not SQLite.
    """
    column_type = BomLine.__table__.c[name].type
    if not isinstance(column_type, String) or column_type.length is None:  # pragma: no cover
        raise RuntimeError(f"bom_lines.{name} is not a bounded String column")
    return column_type.length


_WIDTHS: Mapping[str, int] = {
    name: _column_width(name)
    for name in ("designators", "ref_value", "footprint", "mpn_raw", "mpn_norm", "manufacturer_raw")
}


# ---------------------------------------------------------------------------
# Parsing — pure, never touches a session, never raises on content
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedBomLine:
    """One row of the file, interpreted but not yet stored.

    Field names line up with `bom_lines` where a column exists, so `import_bom`
    is a copy rather than a second interpretation step.
    """

    #: Position among the *emitted* lines, 1-based — what the user saw in KiCad.
    line_no: int
    #: Physical row in the file, 1-based and counting the header and preamble.
    #: Every warning quotes it, because "line 7" and "row 13" are different
    #: numbers the moment a file has a preamble, and the user is looking at rows.
    source_row: int

    designators: str | None
    #: The designator cell expanded: ranges resolved, separators applied.
    #: Duplicates are **kept** — two components sharing a designator is a
    #: schematic error, and deduplicating it would hide the error inside a
    #: quantity that silently no longer matches the board.
    designator_refs: tuple[str, ...]

    qty_per_assembly_milli: int
    qty_source: QtySource
    #: What a quantity column said, in milli-units, when it said anything.
    declared_qty_milli: int | None

    ref_value: str | None
    footprint: str | None
    mpn_raw: str | None
    mpn_norm: str | None
    manufacturer_raw: str | None
    description: str | None
    datasheet: str | None
    is_dnp: bool

    #: The `Value` cell parsed against the quantity its designator prefix
    #: implies, or `None` when there is no such prefix or it did not parse.
    #: **Not stored** — `bom_lines` has no numeric columns, deliberately: a BOM
    #: line is a requirement, not a part, and a parameter belongs to the part it
    #: gets matched to. What this is for is telling a value from a part number;
    #: see the module docstring.
    value: ParsedValue | None
    #: Why a value did not parse (`implausible`, `syntax`, ...), for the report.
    value_parse_error: str | None

    raw_fields: Mapping[str, str]
    warnings: tuple[str, ...] = ()

    @property
    def note(self) -> str | None:
        """This row's warnings, as the text to put on `bom_lines.note`.

        Warnings live on the row and not only in an import log because an import
        log is read once. A quantity disagreement has to be visible to whoever
        opens the BOM three weeks later, next to the number it is about.
        """
        return "; ".join(self.warnings) if self.warnings else None


@dataclass(frozen=True)
class ParsedBom:
    """A whole file, interpreted. Always returned; never an exception."""

    lines: tuple[ParsedBomLine, ...]
    #: Canonical field to the header text that supplied it, so a UI can show
    #: "quantity ← Qnty" and a user can see why a column was ignored.
    columns: Mapping[BomField, str]
    #: Headers no field claimed. Their values are still in every row's
    #: `raw_fields`, so this is a display concern, not a data-loss one.
    unmapped_headers: tuple[str, ...]
    delimiter: str
    #: Rows above the header row (`Source:`, `Date:`, `Component Count:`).
    #: Returned rather than discarded — they name the schematic the BOM came
    #: from, which is what a caller wants for `projects.source_ref`.
    preamble: tuple[tuple[str, ...], ...]
    warnings: tuple[str, ...] = ()

    @property
    def all_warnings(self) -> tuple[str, ...]:
        """File-level warnings plus every row's, each prefixed with its row."""
        return (
            *self.warnings,
            *(
                f"row {line.source_row}: {warning}"
                for line in self.lines
                for warning in line.warnings
            ),
        )


def parse_bom(source: str | bytes) -> ParsedBom:
    """Interpret a KiCad-style BOM export. **Never raises.**

    Byte input is decoded `utf-8-sig` first — the BOM marker Excel prepends is
    what actually arrives from Windows, and a stray `\\ufeff` on the first header
    would silently unmap the designator column. `cp1252` is the fallback because
    that is what the same machines produce when they are *not* being helpful, and
    a lossy `utf-8` decode is the last resort, since a mangled `µ` is a review
    item while a raised `UnicodeDecodeError` is a file the user cannot import.
    """
    warnings: list[str] = []
    text = _decode(source, warnings) if isinstance(source, bytes) else source.lstrip("\ufeff")

    if not text.strip():
        # Not an error: a user who exported the wrong thing gets told so, and an
        # empty import is indistinguishable from a BOM with no components.
        return ParsedBom(
            lines=(),
            columns={},
            unmapped_headers=(),
            delimiter=_DELIMITERS[0],
            preamble=(),
            warnings=(*warnings, "file is empty"),
        )

    probe = _best_probe(text)
    warnings.extend(probe.warnings)

    columns = {bom_field: probe.headers[index] for bom_field, index in probe.mapping.items()}
    lines: list[ParsedBomLine] = []
    for offset, row in enumerate(probe.rows[probe.first_data_row :]):
        if not any(cell.strip() for cell in row):
            # A blank row is layout, not data — the XSL templates emit one
            # between the preamble and the table. It consumes no line number,
            # because `line_no` is what the user saw in KiCad.
            continue
        lines.append(
            _parse_row(
                row,
                mapping=probe.mapping,
                headers=probe.headers,
                line_no=len(lines) + 1,
                source_row=probe.first_data_row + 1 + offset,
            )
        )

    return ParsedBom(
        lines=tuple(lines),
        columns=columns,
        unmapped_headers=probe.unmapped,
        delimiter=probe.delimiter,
        # Nothing is above row 1, so a headerless file has no preamble — and
        # slicing `rows[:0 - 1]` would have handed the caller the whole file
        # minus its last row as one.
        preamble=(
            ()
            if probe.header_index is None
            else tuple(tuple(row) for row in probe.rows[: probe.header_index])
        ),
        warnings=tuple(warnings),
    )


def _decode(source: bytes, warnings: list[str]) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return source.decode(encoding)
        except UnicodeDecodeError:
            continue
    warnings.append("file is not valid UTF-8 or cp1252; undecodable bytes were replaced")
    return source.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class _Probe:
    """One candidate reading of the file: a delimiter and the header it found."""

    delimiter: str
    rows: tuple[tuple[str, ...], ...]
    #: Which row is the header, or `None` for a file that has none — in which
    #: case **every row is data**, including the first. It used to be `0` for
    #: that case, which quietly ate the first component of a headerless file:
    #: `R1,10k,1\nC1,100nF,1` landed one line, and `R1 / 10k` existed nowhere in
    #: the database while the warning only said the header had been guessed.
    header_index: int | None
    headers: tuple[str, ...]
    mapping: Mapping[BomField, int]
    unmapped: tuple[str, ...]
    #: Data rows whose width equals the header's. A tie-break only: a file read
    #: with the wrong delimiter is one column wide, so every row "agrees".
    consistent_rows: int
    warnings: tuple[str, ...] = ()

    @property
    def score(self) -> tuple[int, int]:
        return len(self.mapping), self.consistent_rows

    @property
    def first_data_row(self) -> int:
        """Index into `rows` of the first row to emit as a line."""
        return 0 if self.header_index is None else self.header_index + 1


def _best_probe(text: str) -> _Probe:
    """Read the file with every candidate delimiter; keep the best reading.

    Scored on **recognised columns first**, row-width consistency second. The
    tempting cheap version — count separator characters — gets a semicolon file
    wrong whenever a designator cell holds `"R1,R2,R3"`, which is the common
    case rather than the corner one.
    """
    probes = [_probe(text, delimiter) for delimiter in _DELIMITERS]
    best = max(probes, key=lambda probe: probe.score)
    if best.score == (0, 0) or len(best.mapping) < _MIN_HEADER_FIELDS:
        # No delimiter produced a recognisable header. **The file is then treated
        # as having none at all** rather than sacrificing its first row to be one:
        # every cell reaches `raw_fields_json` under a positional name, so the
        # file lands whole as a worklist instead of being refused *or* quietly
        # short one component, which is the whole contract.
        fallback = probes[0]
        width = max((len(row) for row in fallback.rows), default=0)
        # Synthesised, not borrowed from row 1: real names would claim the first
        # component's cells, and a positional name is also what `_raw_fields`
        # already falls back to for a row wider than its header.
        headers = tuple(f"column_{index + 1}" for index in range(width))
        return _Probe(
            delimiter=fallback.delimiter,
            rows=fallback.rows,
            header_index=None,
            headers=headers,
            mapping={},
            unmapped=headers,
            consistent_rows=0,
            warnings=(
                *fallback.warnings,
                "no recognisable header row; every row was imported as data with "
                "positional column names, and every cell is in the raw fields",
            ),
        )
    return best


def _probe(text: str, delimiter: str) -> _Probe:
    # `newline=""` per the csv docs: it is what keeps a quoted cell containing a
    # newline intact, and the reader handles CRLF itself either way.
    try:
        rows = tuple(
            tuple(row) for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        )
    except csv.Error as error:
        # Never fatal, per this module's contract. `_MAX_FIELD_CHARS` already
        # covers everything the API accepts, so reaching here means a caller fed
        # `parse_bom` something larger still (a file, a future CLI import) — and
        # a warning naming the delimiter that choked is a worklist item, while an
        # exception out of here is a 500 on a file the route said it would take.
        return _Probe(
            delimiter=delimiter,
            rows=(),
            header_index=None,
            headers=(),
            mapping={},
            unmapped=(),
            consistent_rows=0,
            warnings=(f"file could not be read as {delimiter!r}-delimited text: {error}",),
        )
    for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        mapping, unmapped, rivals = _map_headers(row)
        if len(mapping) < _MIN_HEADER_FIELDS:
            continue
        width = len(row)
        return _Probe(
            delimiter=delimiter,
            rows=rows,
            header_index=index,
            headers=row,
            mapping=mapping,
            unmapped=unmapped,
            consistent_rows=sum(1 for later in rows[index + 1 :] if len(later) == width),
            warnings=rivals,
        )
    return _Probe(
        delimiter=delimiter,
        rows=rows,
        header_index=None,
        headers=(),
        mapping={},
        unmapped=(),
        consistent_rows=0,
    )


def _normalize_header(header: str) -> str:
    return _NOT_HEADER.sub("", header.strip().lstrip("\ufeff").casefold())


def _map_headers(
    headers: Sequence[str],
) -> tuple[dict[BomField, int], tuple[str, ...], tuple[str, ...]]:
    """Header row to `{field: column index}`, the headers nothing claimed, and a
    warning for each **rival**: a second column naming a field another column
    already won.

    Two headers claiming one field is resolved by alias priority (`Footprint`
    beats `Package`) and then by column order, never by "last wins" — a file
    with a trailing duplicate column must not have its good column shadowed.

    The rival warnings exist because that rule is invisible otherwise, and for
    one field it is dangerous: a file with `MPN` and `mpn` (or `Part Number`)
    imports the first and drops the second with no signal at all, so a user who
    put the real part number in the second column gets a confidently matched
    wrong line and a clean import report. The losing column's value is still in
    `raw_fields_json`; what was missing was anyone being told to look.
    """
    best: dict[BomField, tuple[int, int]] = {}
    rivals: dict[BomField, list[str]] = {}
    for index, header in enumerate(headers):
        key = _normalize_header(header)
        hit = _ALIAS_LOOKUP.get(key) or _ALIAS_LOOKUP.get(_singular(key))
        if hit is None:
            continue
        bom_field, priority = hit
        current = best.get(bom_field)
        if current is None:
            best[bom_field] = (priority, index)
            continue
        # The *loser* is what the user has to be told about, and which one that is
        # depends on alias priority rather than column order: `Package, Footprint`
        # keeps the second column, `Footprint, Package` the first.
        if priority < current[0]:
            rivals.setdefault(bom_field, []).append(_header_name(headers, current[1]))
            best[bom_field] = (priority, index)
        else:
            rivals.setdefault(bom_field, []).append(_header_name(headers, index))

    mapping = {bom_field: index for bom_field, (_, index) in best.items()}
    claimed = set(mapping.values())
    unmapped = tuple(
        header for index, header in enumerate(headers) if index not in claimed and header.strip()
    )
    warnings = tuple(
        f"more than one column names the {bom_field.value} field:"
        f" used {_header_name(headers, mapping[bom_field])!r},"
        f" ignored {', '.join(repr(name) for name in also)};"
        f" the ignored values are in the raw fields"
        for bom_field, also in rivals.items()
    )
    return mapping, unmapped, warnings


def _header_name(headers: Sequence[str], index: int) -> str:
    """A header for a warning to quote. Positional when the cell is blank, since
    "column 4" is still something a user can find in their file."""
    return headers[index].strip() or f"column {index + 1}"


def _singular(key: str) -> str:
    """`references` → `reference`. Pluralisation is the difference between two
    KiCad versions' header for the same column, so it cannot be a mismatch."""
    return key[:-1] if key.endswith("s") and len(key) > 1 else key


def _parse_row(
    row: Sequence[str],
    *,
    mapping: Mapping[BomField, int],
    headers: Sequence[str],
    line_no: int,
    source_row: int,
) -> ParsedBomLine:
    warnings: list[str] = []
    raw_fields = _raw_fields(row, headers, warnings)

    def cell(bom_field: BomField) -> str | None:
        index = mapping.get(bom_field)
        if index is None or index >= len(row):
            return None
        return _clean(row[index])

    designators_raw = cell(BomField.DESIGNATORS)
    refs = _expand_designators(designators_raw, warnings)
    declared = _declared_qty_milli(cell(BomField.QUANTITY), warnings)
    qty_milli, qty_source = _resolve_qty(declared, refs, warnings)

    ref_value = cell(BomField.VALUE)
    parsed_value, value_error = _parse_ref_value(ref_value, refs)
    mpn_raw = cell(BomField.MPN)

    if not any((designators_raw, ref_value, mpn_raw, cell(BomField.DESCRIPTION))):
        # Kept anyway: something is in this row, and an import that silently
        # dropped rows would be worse than one that lands a row saying so.
        warnings.append("row names no designator, value or part number")

    dnp_cell = cell(BomField.DNP)
    return ParsedBomLine(
        line_no=line_no,
        source_row=source_row,
        designators=_fit(designators_raw, "designators", warnings),
        designator_refs=refs,
        qty_per_assembly_milli=qty_milli,
        qty_source=qty_source,
        declared_qty_milli=declared,
        ref_value=_fit(ref_value, "ref_value", warnings),
        footprint=_fit(cell(BomField.FOOTPRINT), "footprint", warnings),
        mpn_raw=_fit(mpn_raw, "mpn_raw", warnings),
        # A copy of `mpn_raw` and nothing else, per the column's contract. The
        # `Value`-as-part-number fallback lives in `_mpn_candidates`, where it
        # can be used for a lookup without being written down as if the file had
        # said it.
        mpn_norm=_fit(normalized_mpn(mpn_raw), "mpn_norm", warnings),
        manufacturer_raw=_fit(cell(BomField.MANUFACTURER), "manufacturer_raw", warnings),
        description=cell(BomField.DESCRIPTION),
        datasheet=cell(BomField.DATASHEET),
        is_dnp=dnp_cell is not None and dnp_cell.casefold() not in _FALSY,
        value=parsed_value,
        value_parse_error=value_error,
        raw_fields=raw_fields,
        warnings=tuple(warnings),
    )


def normalized_mpn(mpn_raw: str | None) -> str | None:
    """`bom_lines.mpn_norm` for a raw MPN cell. **The only definition of it.**

    Public because import is not the only writer: a curator correcting `mpn_raw`
    through `PUT /api/projects/{id}/bom` has to re-derive the same key, and that
    route used to copy `mpn_raw` through and leave `mpn_norm` holding the old
    text's normalisation — the exact hazard `normalize_mpn`'s docstring names, a
    row written under a different rule than `normalize_mpn(mpn_raw)`, invisible
    to the matcher while looking perfectly correct. Concretely: a line imported
    as `AAA111` and corrected to `LM358DR` re-matched to the part for `AAA111`,
    and a line whose typo'd MPN was fixed to one that *does* exist stayed
    unmatched forever.

    `or None` collapses a cell that normalises to nothing (`"---"`) to NULL, so
    the unmatched-lines index holds it and an empty string is never a lookup key.
    """
    if not mpn_raw:
        return None
    return normalize_mpn(mpn_raw) or None


def _clean(cell: str) -> str | None:
    """A cell's value, or `None` for the several ways a file says "unset".

    `~` is KiCad's own empty-field marker and `-`/`n/a` are what a spreadsheet
    round-trip leaves behind. Collapsing them to `None` here is why a downstream
    "has an MPN" test does not have to know any of that.
    """
    text = cell.strip()
    return None if text.casefold() in _EMPTY_MARKERS else text


def _raw_fields(
    row: Sequence[str], headers: Sequence[str], warnings: list[str]
) -> Mapping[str, str]:
    """The whole source row as `{header: value}`, mapped and unmapped alike.

    Every cell, including ones with a typed column: this is the archive copy,
    and the point of an archive is that it does not depend on which fields this
    version of the schema happened to model. Cells past the header's width get
    positional keys rather than being dropped — a row wider than its header is a
    malformed export, and the extra cells are the evidence of what went wrong.
    """
    fields: dict[str, str] = {}
    for index, cell in enumerate(row):
        text = cell.strip()
        if not text:
            continue
        name = headers[index].strip() if index < len(headers) else f"column_{index + 1}"
        key = name or f"column_{index + 1}"
        if key in fields:
            # A duplicate header would otherwise silently overwrite; suffixing
            # keeps both, and the warning says why the key looks odd.
            warnings.append(f"duplicate column {key!r}; kept as {key}__{index + 1}")
            key = f"{key}__{index + 1}"
        fields[key] = text
    if len(row) > len(headers):
        warnings.append(f"row has {len(row)} cells for {len(headers)} columns")
    return fields


def _fit(value: str | None, column: str, warnings: list[str]) -> str | None:
    """Truncate to the column's declared width, loudly.

    A hundred-capacitor decoupling line really does overflow `designators`.
    Truncating is safe *because* the untruncated text is in `raw_fields_json`,
    which is `Text`: the row keeps everything and only the indexed copy is
    clipped.
    """
    if value is None:
        return None
    width = _WIDTHS[column]
    if len(value) <= width:
        return value
    warnings.append(
        f"{column} is {len(value)} characters, truncated to {width}; "
        f"the full text is kept in the raw fields"
    )
    return value[:width]


def _expand_designators(cell: str | None, warnings: list[str]) -> tuple[str, ...]:
    """`"R1,R2,R5"`, `"R1 R2 R5"` and `"R1-R5"` to a list of references.

    Range expansion is what makes the quantity cross-check meaningful: a tool
    that writes `C1-C4` and `Qty 4` is consistent, and a reader that counts one
    designator would report a disagreement on every such line and train the user
    to ignore the warning. Zero padding is preserved (`R01-R03`), because a
    designator is a string and `R1` is not `R01` on the silkscreen.
    """
    if cell is None:
        return ()
    refs: list[str] = []
    for token in _DESIGNATOR_SPLIT.split(cell):
        if not token:
            continue
        expanded = _expand_range(token, warnings)
        refs.extend(expanded)

    duplicates = sorted(ref for ref, count in Counter(refs).items() if count > 1)
    if duplicates:
        warnings.append(f"designators repeated: {', '.join(duplicates)}")
    return tuple(refs)


def _expand_range(token: str, warnings: list[str]) -> tuple[str, ...]:
    match = _DESIGNATOR_RANGE.match(token)
    if match is None:
        return (token,)
    prefix, start_text, end_prefix, end_text = match.groups()
    if end_prefix and end_prefix != prefix:
        # `R1-C4` is not a range of anything. Keeping the token whole means the
        # count is off by the span, which the quantity check then flags.
        warnings.append(f"{token!r} is not a designator range; kept verbatim")
        return (token,)
    start, end = int(start_text), int(end_text)
    if end < start:
        warnings.append(f"{token!r} runs backwards; kept verbatim")
        return (token,)
    if end - start + 1 > _MAX_RANGE_SPAN:
        warnings.append(f"{token!r} spans more than {_MAX_RANGE_SPAN} designators; kept verbatim")
        return (token,)
    width = len(start_text)
    return tuple(f"{prefix}{number:0{width}d}" for number in range(start, end + 1))


def _declared_qty_milli(cell: str | None, warnings: list[str]) -> int | None:
    """A quantity column as milli-units, or `None` if it did not say a number.

    `Decimal`, not `float`: `0.5` metres of wire is a legal line quantity and
    `int(0.5 * 1000)` is not reliably 500. A cell containing a comma is
    **refused rather than guessed** — `1,5` is one and a half in half of Europe
    and fifteen hundred in the other half — which is the same rule the value
    parser applies to ambiguous input.
    """
    if cell is None:
        return None
    text = re.sub(r"[\s\u00a0\u202f\u2009]+", "", cell)
    if "," in text:
        warnings.append(f"quantity {cell!r} contains a comma; ambiguous decimal, ignored")
        return None
    try:
        quantity = Decimal(text)
    except InvalidOperation:
        warnings.append(f"quantity {cell!r} is not a number; ignored")
        return None
    if quantity <= 0:
        warnings.append(f"quantity {cell!r} is not positive; ignored")
        return None
    milli = int(quantity * 1000)
    if milli == 0:
        warnings.append(f"quantity {cell!r} is below one milli-unit; ignored")
        return None
    return milli


def _resolve_qty(
    declared: int | None, refs: Sequence[str], warnings: list[str]
) -> tuple[int, QtySource]:
    """One quantity from two possibly disagreeing statements.

    **A disagreement is never silently resolved in favour of one side.** It is
    recorded on the row (`ParsedBomLine.note`, which `import_bom` copies to
    `bom_lines.note`) with both numbers, because it is real evidence about the
    export: a designator list truncated by a spreadsheet, a range this reader
    did not expand, a hand-edit that removed a component but not the count.

    A number still has to be written — `qty_per_assembly_milli` is NOT NULL and
    a line with no demand is indistinguishable from a DNP — so the rule is
    **take the larger**. It is asymmetric on purpose: over-stating demand
    over-reserves stock, which is visible in the shortage report and released
    with one action, while under-stating it is discovered at the bench with half
    a board populated. Both numbers survive in `declared_qty_milli` and the
    designator list, so nothing is lost either way.
    """
    counted = len(refs) * 1000 if refs else None
    if declared is not None and counted is not None and declared != counted:
        warnings.append(
            f"quantity column says {declared / 1000:g} but {len(refs)}"
            f" designator{'' if len(refs) == 1 else 's'} listed;"
            f" used {max(declared, counted) / 1000:g}"
        )
        return max(declared, counted), QtySource.DISAGREEMENT_MAX
    if counted is not None:
        return counted, QtySource.DESIGNATOR_COUNT
    if declared is not None:
        return declared, QtySource.DECLARED
    warnings.append("no quantity column and no designators; assumed one per assembly")
    return _FALLBACK_QTY_MILLI, QtySource.FALLBACK


def _designator_prefix(refs: Sequence[str]) -> str | None:
    """The shared letter prefix of a designator list, uppercased.

    `None` when the list is empty or mixed. Mixed matters: a grouped line
    covering `R1,C2` is a malformed export, and picking either prefix would
    parse the value against a quantity half the line contradicts.
    """
    prefixes = {
        match.group(1).upper() for ref in refs if (match := re.match(r"^([A-Za-z_]+)", ref))
    }
    return prefixes.pop() if len(prefixes) == 1 else None


def _parse_ref_value(
    ref_value: str | None, refs: Sequence[str]
) -> tuple[ParsedValue | None, str | None]:
    """Parse a `Value` cell against the quantity its designators imply.

    Returns `(None, None)` when no parse was *attempted* — no value, or a
    designator class whose value is a part name rather than a number — and
    `(None, reason)` when one was attempted and refused. The distinction is what
    `_mpn_candidates` needs: "not a value" is only evidence that a cell might be
    a part number if somebody actually asked the question.
    """
    if not ref_value or not refs:
        return None, None
    prefix = _designator_prefix(refs)
    unit = None if prefix is None else _PASSIVE_QUANTITY.get(prefix)
    if unit is None:
        return None, None
    try:
        return parse_electronics_value(ref_value, unit), None
    except ValueParseError as error:
        # Never fatal. A refused value is a curation item — `0603` in the value
        # column, a `10k/1%` this grammar does not accept — and the raw text is
        # on the row for a human or a better parser.
        return None, error.reason


#: Every quantity the parser knows, for the sweep in `_reads_as_a_quantity`.
#: Read off the library rather than listed here, so a quantity added there
#: strengthens this gate instead of leaving a hole in it. Sorted for a
#: deterministic answer to "which quantity read it".
_ALL_QUANTITIES: tuple[str, ...] = tuple(sorted(known_quantities()))

#: Cells that are a description rather than a token. A part number is one token;
#: `10k 1%`, `10k, 1%` and `RES 10K 1% 0603` are sentences about a part. This
#: matters because `normalize_mpn` deletes exactly these characters, so the
#: sentence collapses into something that looks like a part number and *is* one:
#: `10k 1%` becomes the key `10k1`, which is a real E96 MPN (`10K1`, 10.1 kΩ).
#: Reproduced matching a 10.0 kΩ line to 10.1 kΩ stock that way.
_NOT_ONE_TOKEN = re.compile(r"[\s,;/%]")


@functools.lru_cache(maxsize=4096)
def _reads_as_a_quantity(ref_value: str) -> str | None:
    """The first known quantity that reads this whole cell as a value, or `None`.

    The generalisation of `_parse_ref_value` to the case where the designators do
    **not** say what quantity the cell expresses — no designator column, an empty
    designator cell, or a prefix outside `R`/`C`/`L`. `_mpn_candidates` used to
    treat all of those as "no parse was attempted, so the cell may be a part
    number", which meant the value parser was only a gate when the designator
    happened to start with one of three letters: `RN1` (a resistor network) and
    `VR1` (a potentiometer) with a `Value` of `10k` were both matched to a chip
    resistor named `10K`, and a file with no `Reference` column at all matched
    every passive line that way.

    A successful parse under *any* quantity is the right test, and it separates
    the two populations cleanly: `10k`, `4k7`, `100nF`, `0R22`, `1M`, `22p`,
    `16MHz` and even a bare `0603` all read as a value under something, while
    `LM358N`, `74HC595`, `1N4148`, `STM32F103C8T6` and `RC0603FR-0710KL` read as
    a value under nothing. A *refusal* is not used here — `implausible` and
    `unit_mismatch` mean the text was read as a quantity and rejected, which the
    sweep catches through whichever quantity accepts it instead.

    Cached because a BOM repeats its values (a hundred `100nF` lines is one
    decoupling net) and `_mpn_candidates` runs twice per line — once to collect
    keys, once to apply them.
    """
    for quantity in _ALL_QUANTITIES:
        try:
            parse_electronics_value(ref_value, quantity)
        except ValueParseError:
            continue
        return quantity
    return None


#: Parse failures that mean "this cell is not a quantity at all", and therefore
#: might be a part number. **A refusal is not uniform evidence**, which is why
#: this is a list and not `error is not None`:
#:
#: * `syntax` — the grammar found no number in it. `GRM188R71C104KA01D`.
#: * `unknown_unit` — a number followed by something that is not a unit in any
#:   quantity. `RC0603FR-0710KL` reads as `0710` and the "unit" `KL`.
#:
#: Everything else stays out, deliberately. `implausible` (`1M` under `C`) and
#: `unit_mismatch` (`100nH` under `C`) both mean the text *was* read as a
#: quantity and rejected — a bad value, not a part number — and treating those
#: as part numbers is how `1M` becomes a match against somebody's part named
#: `1M`. Unlisted reasons are treated as "still a value", the conservative side.
_MPN_SHAPED_FAILURES = frozenset({"syntax", "unknown_unit"})


def _mpn_candidates(line: ParsedBomLine) -> tuple[tuple[BomField, str], ...]:
    """Normalised lookup keys for this line, strongest first.

    The `Value` fallback is what matches an IC whose BOM has no MPN column at
    all (`U3` = `LM358N`, the single most common shape of a hobby BOM).

    **The value parser is the entire gate**, and it is the reason this module
    depends on it. `normalize_mpn("10k")` is a perfectly good lookup key, and
    somebody's catalogue really does contain a part named `10K`; matching an
    `R1,R2 / 10k` line to it puts a resistor of unknown tolerance and unknown
    power rating into a build and calls the line identified. `_parse_ref_value`
    answering "that is ten kilohms, because the designators say R" is what
    stands between those two cases.

    Deliberately **not** gated on the designator prefix instead, though that
    would refuse the same bad match: a resistor line whose `Value` really does
    hold `RC0603FR-0710KL` is common in BOMs exported from a schematic where
    somebody typed the part number into the wrong field, and the parser refusing
    that text on *syntax* is exactly the evidence that distinguishes it from a
    value. A prefix rule cannot see the difference and would leave those lines
    unmatched forever.

    Three conditions, and each closes a hole that was reproduced:

    1. the implied-quantity parse, when the designators implied one, refused in
       an MPN-shaped way (`_MPN_SHAPED_FAILURES`);
    2. **no** quantity reads the cell as a value (`_reads_as_a_quantity`) — the
       original rule asked only about the implied quantity, so a line with no
       designator column, a blank designator cell or an `RN1`/`VR1` prefix
       skipped the gate entirely and matched `10k` against a part named `10K`;
    3. the cell is one token (`_NOT_ONE_TOKEN`) — `normalize_mpn` deletes the
       spaces and percent sign in `10k 1%`, and the key that survives is a real
       part number belonging to a different resistance.
    """
    candidates: list[tuple[BomField, str]] = []
    if line.mpn_norm:
        candidates.append((BomField.MPN, line.mpn_norm))

    if line.mpn_raw or not line.ref_value:
        return tuple(candidates)

    refused_as_an_mpn = (
        line.value is None
        and (line.value_parse_error is None or line.value_parse_error in _MPN_SHAPED_FAILURES)
        and _reads_as_a_quantity(line.ref_value) is None
        and _NOT_ONE_TOKEN.search(line.ref_value) is None
    )
    if refused_as_an_mpn:
        key = normalize_mpn(line.ref_value)
        if key:
            candidates.append((BomField.VALUE, key))
    return tuple(candidates)


# ---------------------------------------------------------------------------
# Import — the only part that touches a session
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BomImportResult:
    """What an import did, in the terms a user asks about afterwards."""

    project_id: int
    lines: tuple[BomLine, ...] = ()
    #: Lines given a `part_id` by an exact unique MPN hit. `is_match_confirmed`
    #: is false on every one of them — a machine match is a suggestion.
    matched_count: int = 0
    unmatched_count: int = 0
    dnp_count: int = 0
    #: Keys that hit more than one `parts` row, so no match was made. Two rows
    #: sharing an `mpn_norm` differ by manufacturer (`uq_parts_mpn_norm_
    #: manufacturer`), and choosing between them is a curation decision.
    ambiguous_keys: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def line_count(self) -> int:
        return len(self.lines)


def import_bom(
    session: Session, project: Project, parsed: ParsedBom, *, match: bool = True
) -> BomImportResult:
    """Write `parsed` into `project` as `bom_lines`. **Appends; never replaces.**

    Re-import as *reconciliation* — recognising that this file is a new revision
    of lines already here — is deliberately not implemented. It needs a line
    identity rule the file does not supply (designators change, line numbers
    renumber), and getting it wrong deletes `bom_lines` rows that
    `stock_allocations.bom_line_id` points at, silently detaching the record of
    what a previous build actually consumed. Appending is honest and reversible;
    a wrong merge is neither.

    Line numbers continue after whatever the project already has, so importing a
    second sheet into one project does not produce two "line 1"s.
    """
    base = _highest_line_no(session, project.id)
    rows = [
        BomLine(
            project_id=project.id,
            line_no=base + parsed_line.line_no,
            designators=parsed_line.designators,
            qty_per_assembly_milli=parsed_line.qty_per_assembly_milli,
            is_dnp=parsed_line.is_dnp,
            ref_value=parsed_line.ref_value,
            footprint=parsed_line.footprint,
            mpn_raw=parsed_line.mpn_raw,
            mpn_norm=parsed_line.mpn_norm,
            manufacturer_raw=parsed_line.manufacturer_raw,
            description=parsed_line.description,
            # Sorted keys so a re-export of the same file produces byte-identical
            # JSON, which is what makes a diff between two imports readable.
            raw_fields_json=json.dumps(dict(sorted(parsed_line.raw_fields.items()))),
            note=parsed_line.note,
        )
        for parsed_line in parsed.lines
    ]
    session.add_all(rows)
    session.flush()

    warnings = list(parsed.all_warnings)
    if base:
        warnings.append(
            f"project already had lines up to {base}; this import is numbered from {base + 1}"
        )

    matched, ambiguous = (
        _match_lines(session, list(zip(parsed.lines, rows, strict=True))) if match else (0, ())
    )
    return BomImportResult(
        project_id=project.id,
        lines=tuple(rows),
        matched_count=matched,
        unmatched_count=len(rows) - matched,
        dnp_count=sum(1 for line in rows if line.is_dnp),
        ambiguous_keys=ambiguous,
        warnings=tuple(warnings),
    )


def rematch_project(session: Session, project_id: int) -> int:
    """Re-run the matcher over a project's unmatched lines. Returns how many hit.

    The point of keeping every imported field verbatim: parts created since the
    import — by a scan, by a curation pass — make previously unmatchable lines
    matchable, and this is the pass that notices. Reads through
    `ix_bom_lines_unmatched`, which exists for exactly this worklist.

    Candidates are rebuilt from the stored columns rather than from a `ParsedBom`
    that no longer exists, which is why `_mpn_candidates` takes a
    `ParsedBomLine`: the reconstruction goes through the same function, so a
    rule that changes changes for both paths at once.
    """
    unmatched = (
        session.execute(
            select(BomLine)
            .where(BomLine.project_id == project_id, BomLine.part_id.is_(None))
            .order_by(BomLine.line_no, BomLine.id)
        )
        .scalars()
        .all()
    )
    pairs = [(_reparse_stored(row), row) for row in unmatched]
    matched, _ = _match_lines(session, pairs)
    return matched


def _highest_line_no(session: Session, project_id: int) -> int:
    """The project's largest existing `line_no`, or 0.

    A scalar aggregate rather than loading the lines: importing a second sheet
    into a project that already holds a 400-line BOM must not read 400 rows to
    learn one number.
    """
    return int(
        session.execute(
            select(func.coalesce(func.max(BomLine.line_no), 0)).where(
                BomLine.project_id == project_id
            )
        ).scalar_one()
    )


def _reparse_stored(row: BomLine) -> ParsedBomLine:
    """A stored line, back in the shape the matcher understands.

    Only the fields matching consults are reconstructed; everything else is
    filled with what it would have been. This exists so there is **one**
    candidate rule rather than a fresh-import one and a rematch one that drift
    into disagreeing about which lines are safe to match.
    """
    warnings: list[str] = []
    refs = _expand_designators(row.designators, warnings)
    value, value_error = _parse_ref_value(row.ref_value, refs)
    return ParsedBomLine(
        line_no=row.line_no,
        source_row=row.line_no,
        designators=row.designators,
        designator_refs=refs,
        qty_per_assembly_milli=row.qty_per_assembly_milli,
        qty_source=QtySource.DECLARED,
        declared_qty_milli=row.qty_per_assembly_milli,
        ref_value=row.ref_value,
        footprint=row.footprint,
        mpn_raw=row.mpn_raw,
        mpn_norm=row.mpn_norm,
        manufacturer_raw=row.manufacturer_raw,
        description=row.description,
        datasheet=None,
        is_dnp=row.is_dnp,
        value=value,
        value_parse_error=value_error,
        raw_fields={},
    )


def _match_lines(
    session: Session, pairs: Sequence[tuple[ParsedBomLine, BomLine]]
) -> tuple[int, tuple[str, ...]]:
    """Set `part_id` on every line with a unique exact `mpn_norm` hit.

    One query for the whole BOM, not one per line: a 300-line BOM against a
    catalogue is otherwise 300 round trips for a result an `IN` clause gives in
    one, and `parts.mpn_norm` is indexed for it.

    **Never sets `is_match_confirmed`.** An exact normalised equality is strong
    evidence and still not a human's agreement, and the column exists to keep
    those two things apart — the same rule that forbids auto-accepting an OCR'd
    part number.
    """
    keys = {key for parsed_line, _ in pairs for _, key in _mpn_candidates(parsed_line)}
    by_key = _parts_by_mpn_norm(session, keys)

    matched = 0
    ambiguous: dict[str, None] = {}
    for parsed_line, row in pairs:
        for source, key in _mpn_candidates(parsed_line):
            hits = by_key.get(key, ())
            if len(hits) == 1:
                row.part_id = hits[0]
                matched += 1
                if source is BomField.VALUE:
                    # Said out loud on the row, because a reviewer confirming
                    # this match needs to know it came off the Value cell rather
                    # than a part-number column — a weaker claim, even though
                    # the lookup itself was an exact equality.
                    row.note = _append_note(row.note, "matched on the Value column")
                break
            if len(hits) > 1:
                # Stop rather than trying the weaker candidate: an ambiguous
                # strong signal must not be resolved by falling through to a
                # guess, and asking the user is a fine outcome.
                ambiguous.setdefault(key, None)
                break
    session.flush()
    return matched, tuple(ambiguous)


def _parts_by_mpn_norm(session: Session, keys: set[str]) -> Mapping[str, tuple[int, ...]]:
    """`mpn_norm` to the part ids holding it, inactive parts excluded.

    Excluding `is_active = false` is deliberate: a retired part is the wrong
    thing to allocate a new build from, and leaving the line unmatched puts the
    decision in front of the user instead of quietly reviving it. Ordered by id
    so an ambiguous candidate list is stable between runs.
    """
    if not keys:
        return {}
    rows = session.execute(
        select(Part.mpn_norm, Part.id)
        .where(Part.mpn_norm.in_(keys), Part.is_active.is_(True))
        .order_by(Part.id)
    ).all()
    by_key: dict[str, tuple[int, ...]] = {}
    for mpn_norm, part_id in rows:
        if mpn_norm is None:  # pragma: no cover — excluded by the IN clause
            continue
        by_key[mpn_norm] = (*by_key.get(mpn_norm, ()), int(part_id))
    return by_key


def _append_note(existing: str | None, addition: str) -> str:
    """Add a line to `bom_lines.note` without losing what import already wrote."""
    return f"{existing}; {addition}" if existing else addition
