"""The one thing the codec cannot check about itself.

`idcodec.shortid.format_display` renders a cosmetic type prefix — `BIN 4K7T-92M8`
— from a table keyed by `EntityType`'s *values*, spelled out as plain strings.
It has to be spelled out: `idcodec` depends on nothing but the standard library,
so it cannot import `app.models.enums`, and dragging the enum across would undo
the whole point of the split.

The cost of that is a table that can silently fall behind the enum. It does not
crash — `format_display` upper-cases an unknown type — it just prints
`STOCK_LOT 4K7T-92M8` on a label instead of `LOT 4K7T-92M8`, which nobody
notices until the labels are printed. This test is the drift alarm, and it lives
here because this is the only side that can see both halves.
"""

from __future__ import annotations

from idcodec.shortid import DISPLAY_PREFIXES

from app.models.enums import EntityType
from app.services import shortid


def test_every_entity_type_has_a_display_prefix() -> None:
    missing = [member.value for member in EntityType if member.value not in DISPLAY_PREFIXES]
    assert not missing, f"add these to idcodec.shortid.DISPLAY_PREFIXES: {missing}"


def test_the_prefix_table_names_no_type_that_does_not_exist() -> None:
    """The other direction. A prefix for a removed type is dead weight that reads
    as a supported kind of object."""
    known = {member.value for member in EntityType}
    assert set(DISPLAY_PREFIXES) <= known


def test_an_entity_type_member_formats_the_same_as_its_value() -> None:
    """`EntityType` is a `StrEnum`, so passing a member where the codec's
    signature says `str` must work — that is what keeps every existing
    `format_display(code, EntityType.LOCATION)` call site untouched."""
    for member in EntityType:
        assert shortid.format_display("4K7T92M8", member) == shortid.format_display(
            "4K7T92M8", member.value
        )
