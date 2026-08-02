"""`LabelSpec` -> `PIL.Image`, entirely offline: no database, no Alembic, no
print backend beyond `FileBackend` writing to a temp directory.

`app.services.label_rendering` is the "abstraction matters more than any one
backend" half of the label-printing design, so it is exercised directly here
rather than through a route — a test that had to go through HTTP and a real
sheet job to check whether a QR appears at 16 mm would be testing three
layers to answer a one-layer question.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.models.enums import LabelTemplate
from app.services.label_backends import FileBackend, PdfSheetBackend
from app.services.label_rendering import (
    LabelFields,
    LabelSpec,
    _ellipsised,
    _font_for_role,
    compute_card_layout,
    include_qr,
    mm_to_px,
    render_card_image,
)

_PAYLOAD = "https://almagest.example/s/4K7T92M8"


def _spec(
    width_mm: float,
    height_mm: float,
    *,
    dpi: int = 300,
    qr: bool = True,
    template: LabelTemplate = LabelTemplate.DRAWER_CARD,
    fields: LabelFields | None = None,
    outlined: bool = False,
) -> LabelSpec:
    return LabelSpec(
        template=template,
        width_mm=width_mm,
        height_mm=height_mm,
        dpi=dpi,
        fields=fields or LabelFields(primary="A1", secondary="4K7T-92M8", tertiary="Cabinet"),
        qr_payload=_PAYLOAD if qr else None,
        outlined=outlined,
    )


# ---------------------------------------------------------------------------
# Card dimensions: mm at a given DPI, exactly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("width_mm", "height_mm", "dpi"),
    [
        (40.0, 18.0, 300),  # PLAN.md's worked example, at the DPI it recommends
        (18.0, 87.0, 203),  # a Raaco strip, at the marginal thermal DPI
        (46.0, 22.0, 600),  # an un-shrunk front, at a high-DPI laser setting
    ],
)
def test_png_dimensions_match_requested_mm_at_dpi(
    tmp_path: Path, width_mm: float, height_mm: float, dpi: int
) -> None:
    spec = _spec(width_mm, height_mm, dpi=dpi)
    image = render_card_image(spec)
    expected = (mm_to_px(width_mm, dpi), mm_to_px(height_mm, dpi))
    assert image.size == expected

    backend = FileBackend(tmp_path, dpi=dpi)
    backend.print(image, spec)
    with Image.open(backend.output_dir / "card-001.png") as saved:
        assert saved.size == expected


def test_a_higher_dpi_produces_more_pixels_for_the_same_card() -> None:
    low = render_card_image(_spec(40.0, 18.0, dpi=150))
    high = render_card_image(_spec(40.0, 18.0, dpi=600))
    assert high.size[0] > low.size[0]
    assert high.size[1] > low.size[1]


# ---------------------------------------------------------------------------
# QR inclusion: a property of card geometry, never a global switch
# ---------------------------------------------------------------------------


def test_qr_included_at_exactly_the_16mm_threshold() -> None:
    assert include_qr(_spec(16.0, 40.0)) is True


def test_qr_omitted_just_below_the_16mm_threshold() -> None:
    assert include_qr(_spec(15.9, 40.0)) is False


def test_qr_omitted_on_a_40x12_gridfinity_slot() -> None:
    """PLAN.md's own counter-example: no room for a QR reliable at reading
    distance without crowding out the text."""
    assert include_qr(_spec(40.0, 12.0)) is False


def test_qr_included_on_an_18x87_raaco_strip() -> None:
    assert include_qr(_spec(18.0, 87.0)) is True


def test_no_qr_payload_means_no_qr_regardless_of_size() -> None:
    assert include_qr(_spec(40.0, 40.0, qr=False)) is False


def test_the_short_dimension_gates_inclusion_not_the_long_one() -> None:
    """A long, narrow strip (87 mm) does not save a card whose *short* side
    (12 mm) is what actually has to fit a 13 mm-square QR."""
    assert include_qr(_spec(87.0, 12.0)) is False


# ---------------------------------------------------------------------------
# Card content: what compute_card_layout decides to draw
# ---------------------------------------------------------------------------


def test_drawer_card_shows_its_short_id_unconditionally_on_the_qr() -> None:
    """The short id is its own always-shown text block — unlike a part QR's
    caption, it never depends on whether a QR is drawn at all."""
    fields = LabelFields(primary="A1", secondary="4K7T-92M8", tertiary="Cabinet A")
    spec = _spec(10.0, 10.0, fields=fields)  # too small for a QR
    layout = compute_card_layout(spec)
    assert layout.qr_payload is None
    assert [block.text for block in layout.text] == ["A1", "4K7T-92M8", "Cabinet A"]


def test_caption_is_dropped_when_the_qr_is_omitted() -> None:
    fields = LabelFields(primary="ATMEGA328P-PU", caption="ATMEGA328P-PU")
    spec = _spec(10.0, 10.0, template=LabelTemplate.PART_LOT, fields=fields)
    layout = compute_card_layout(spec)
    assert layout.qr_payload is None
    assert all(block.role != "caption" for block in layout.text)


def test_the_bare_mpn_is_captioned_under_every_part_qr() -> None:
    """docs/PLAN.md, "Label printing": "print the bare MPN as text under
    every part QR" — the zero-dependency fallback when a manual
    manufacturer-site search is the only tool at hand."""
    fields = LabelFields(primary="ATMEGA328P-PU", caption="ATMEGA328P-PU")
    spec = _spec(40.0, 40.0, template=LabelTemplate.PART_LOT, fields=fields)
    layout = compute_card_layout(spec)
    assert layout.qr_payload is not None
    captions = [block.text for block in layout.text if block.role == "caption"]
    assert captions == ["ATMEGA328P-PU"]


def test_a_caption_never_appears_without_a_qr_even_if_the_field_is_set() -> None:
    """A caption is never filler for `primary`/`secondary` — it exists solely
    to repeat the MPN under a QR, so setting it has no effect when the card
    is too small to show one."""
    fields = LabelFields(primary="ATMEGA328P-PU", caption="ATMEGA328P-PU")
    small = compute_card_layout(_spec(10.0, 10.0, template=LabelTemplate.PART_LOT, fields=fields))
    large = compute_card_layout(_spec(40.0, 40.0, template=LabelTemplate.PART_LOT, fields=fields))
    assert len(small.text) == len(large.text) - 1


# ---------------------------------------------------------------------------
# Spanning cards: the outline is exactly what the spec says it is
# ---------------------------------------------------------------------------


def test_outline_follows_the_spec_flag_in_either_direction() -> None:
    assert compute_card_layout(_spec(80.0, 18.0, outlined=True)).outlined is True
    assert compute_card_layout(_spec(80.0, 18.0, outlined=False)).outlined is False


def test_rendering_a_spanning_card_does_not_change_its_pixel_size() -> None:
    """The outline is drawn *inside* the card's own bounds — a spanning card
    is bigger because its `width_mm`/`height_mm` already are, not because the
    outline adds any margin of its own."""
    spanning = render_card_image(_spec(80.0, 18.0, dpi=300, outlined=True))
    assert spanning.size == (mm_to_px(80.0, 300), mm_to_px(18.0, 300))


# ---------------------------------------------------------------------------
# Backends: capabilities, and one file per print() call in order
# ---------------------------------------------------------------------------


def test_file_backend_writes_one_png_per_card_in_call_order(tmp_path: Path) -> None:
    backend = FileBackend(tmp_path)
    for label in ("A1", "A2", "A3"):
        spec = _spec(40.0, 18.0, fields=LabelFields(primary=label))
        backend.print(render_card_image(spec), spec)
    files = sorted(p.name for p in backend.output_dir.iterdir())
    assert files == ["card-001.png", "card-002.png", "card-003.png"]


def test_file_backend_reports_no_cutter(tmp_path: Path) -> None:
    backend = FileBackend(tmp_path)
    assert backend.capabilities.cut is False
    assert backend.capabilities.widths_mm == ()


def test_pdf_sheet_backend_reports_a_cutter(tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    backend = PdfSheetBackend(output, cell_width_mm=40.0, cell_height_mm=18.0)
    assert backend.capabilities.cut is True


def test_pdf_sheet_backend_writes_a_nonempty_pdf(tmp_path: Path) -> None:
    output = tmp_path / "sheet.pdf"
    backend = PdfSheetBackend(output, cell_width_mm=40.0, cell_height_mm=18.0)
    spec = _spec(40.0, 18.0)
    backend.print(render_card_image(spec), spec)
    saved = backend.finalize()
    assert saved == output
    assert saved.exists()
    assert saved.stat().st_size > 0


# ---------------------------------------------------------------------------
# Ink stays on the card, and off the QR
# ---------------------------------------------------------------------------

#: A five-level path is ordinary, not pathological: room / wall / rack / shelf /
#: cabinet is exactly what `label_path` holds for a drawer in a real workshop.
_DEEP_PATH = "Workshop / North wall / Rack 2 / Shelf 3 / Cabinet A"


def _ink_columns(image: Image.Image) -> list[int]:
    """x of every non-white pixel. Cheap enough at these card sizes."""
    pixels = image.load()
    assert pixels is not None
    width, height = image.size
    return [x for y in range(height) for x in range(width) if pixels[x, y] != (255, 255, 255)]


def test_a_long_path_does_not_draw_over_the_qr() -> None:
    """The failure that matters: ink in the QR's data and timing region means
    the code does not scan, and the whole point of the card is that it does.

    The QR occupies the right-hand square, so nothing but the QR itself may put
    ink there. Compared against the same card with no text at all, so the
    assertion is about the *text* and not about the QR's own pixels.
    """
    geometry = {"width_mm": 40.0, "height_mm": 18.0}
    qr_only = render_card_image(
        _spec(**geometry, fields=LabelFields(primary="", secondary=None, tertiary=None))
    )
    with_text = render_card_image(
        _spec(
            **geometry,
            fields=LabelFields(primary="A1", secondary="4K7T-92M8", tertiary=_DEEP_PATH),
        )
    )

    qr_left = min(_ink_columns(qr_only))
    text_ink = [x for x in _ink_columns(with_text) if x >= qr_left]
    qr_ink = [x for x in _ink_columns(qr_only) if x >= qr_left]

    # Every pixel at or right of the QR's left edge belongs to the QR, so the
    # two counts match. Text spilling in raises the first above the second.
    assert len(text_ink) == len(qr_ink)


def test_text_too_wide_is_ellipsised_rather_than_cut_mid_word() -> None:
    """PIL clips at the image edge, so an over-wide block never *bleeds* — it is
    silently truncated, which on a card reads as a printing fault rather than as
    "there was more". The tail is dropped with an ellipsis instead.

    Truncating from the right is deliberate: for a `label_path` the start names
    the room and the cabinet, which is the half that tells somebody holding the
    card which wall to walk to.
    """
    image = Image.new("RGB", (200, 40), "white")
    draw = ImageDraw.Draw(image)
    font, _ = _font_for_role("tertiary", 200)

    fits = _ellipsised(draw, "Workshop", font, 400)
    shortened = _ellipsised(draw, _DEEP_PATH, font, 100)

    assert fits == "Workshop"
    assert shortened.endswith("…")
    assert _DEEP_PATH.startswith(shortened[:-1])
    assert draw.textlength(shortened, font=font) <= 100
    # And a width that cannot hold even the ellipsis yields nothing at all,
    # rather than a stray glyph on the card.
    assert _ellipsised(draw, _DEEP_PATH, font, 1) == ""
