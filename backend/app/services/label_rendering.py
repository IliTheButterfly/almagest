"""Pure label-card rendering: `LabelSpec` -> `PIL.Image`, nothing else.

Kept free of both the ORM and the print backends, on purpose — the same
separation `app.services.capacity` draws between a pure strategy and its
DB-facing loaders. That is what lets `tests/unit/test_label_rendering.py`
render a card and inspect exactly what it contains with no database, no
Alembic migration, and no backend in the loop at all.

Two things are deliberately split apart:

* `compute_card_layout` decides **what** belongs on a card — which text
  blocks, whether the QR fits, whether an outline is called for — as plain
  data, with no PIL import in sight. That is the piece worth asserting on
  directly: "is the QR included", "is the MPN captioned" are geometry/content
  questions, not pixel questions, and a test should not have to decode a
  bitmap to answer them.
* `render_card_image` turns that layout into pixels. It is the only function
  here that touches `PIL`/`segno`, and it is intentionally dumb — everything
  it draws, it drew because `compute_card_layout` already decided to.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import segno
from PIL import Image, ImageDraw, ImageFont

from app.models.enums import LabelTemplate

MM_PER_INCH = 25.4

#: A ~30-char `{base_url}/s/{short_id}` payload is QR version 2-3 at ECC-M,
#: which needs roughly 13 mm square including its 4-module quiet zone
#: (docs/PLAN.md, "Label printing"). 16 mm is that requirement plus a little
#: headroom for card margins, and it is a property of the *card*, never a
#: global on/off switch — a Raaco-style 18x87 mm strip clears it; a 40x12 mm
#: Gridfinity slot does not.
MIN_QR_DIMENSION_MM = 16.0


def mm_to_px(length_mm: float, dpi: int) -> int:
    """Round rather than floor/ceil: `FileBackend`'s dimension check compares
    against exactly this, and rounding is the only choice under which
    `mm_to_px` and its own inverse agree at the millimetre values PLAN.md's
    worked examples actually use (e.g. 40 mm at 300 dpi is exactly 472.44...
    px either way, so the tie-breaking rule has to be fixed and shared)."""
    return max(round(length_mm / MM_PER_INCH * dpi), 1)


@dataclass(frozen=True)
class LabelFields:
    """A card's text content, already resolved from the database.

    Nothing downstream of this ever reads a request body — see
    `app.services.labels.resolve_label_fields` — so there is no field here a
    stale or client-supplied string could occupy even by accident.
    """

    #: Large, top-left. `slot_label` for a drawer card, `name` for a cabinet
    #: card, the MPN for a (future) part/lot card.
    primary: str
    #: `short_id`, Crockford-grouped, for the two location templates.
    secondary: str | None = None
    #: A breadcrumb (drawer card) or the full `label_path` (cabinet card).
    tertiary: str | None = None
    #: Drawn under the QR specifically, and only when a QR is actually
    #: included — never a substitute for `primary`/`secondary` elsewhere on
    #: the card. The MPN is the only caption PLAN.md calls for: "print the
    #: bare MPN as text under every part QR", the zero-dependency fallback
    #: when a manual search is the only tool at hand.
    caption: str | None = None


@dataclass(frozen=True)
class LabelSpec:
    """Template + target size + DPI + payload — pure data, per
    `docs/PLAN.md`'s architecture sketch. No PIL, no reportlab, no database
    handle, so two specs compare equal by value and a spec can cross a queue
    or a process boundary with no adapter.
    """

    template: LabelTemplate
    width_mm: float
    height_mm: float
    dpi: int
    fields: LabelFields
    #: `{base_url}/s/{short_id}` — the same payload as the NFC tag, one
    #: payload two carriers. `None` only for a template with nothing to point
    #: at (there is none today; kept optional so the QR gate below is a
    #: property of the spec rather than an assumption every caller repeats).
    qr_payload: str | None = None
    #: Set for a slot whose footprint spans more than one base grid cell — the
    #: visible outline PLAN.md calls for "so cuts still line up" against the
    #: sheet's fixed-pitch crop marks.
    outlined: bool = False
    #: 0-based position in the sheet's row-major grid, in **base cells**, not
    #: pixels or mm — `PdfSheetBackend` is the only consumer, and it multiplies
    #: by its own fixed cell pitch. Irrelevant to `FileBackend`, which prints
    #: one card at a time with no sheet to place it on.
    grid_row: int = 0
    grid_col: int = 0


def include_qr(spec: LabelSpec) -> bool:
    """QR inclusion is conditional on card geometry, never a global switch."""
    return spec.qr_payload is not None and min(spec.width_mm, spec.height_mm) >= MIN_QR_DIMENSION_MM


@dataclass(frozen=True)
class TextBlock:
    text: str
    #: "primary" | "secondary" | "tertiary" | "caption" — drives both font
    #: size and the caption's QR-conditional inclusion below.
    role: str


@dataclass(frozen=True)
class CardLayout:
    """What belongs on one card, independent of PIL entirely.

    This is the thing worth asserting on: "is the QR here", "is the MPN
    captioned under it" are answered by inspecting this dataclass, with no
    image decoding involved.
    """

    width_mm: float
    height_mm: float
    text: tuple[TextBlock, ...]
    qr_payload: str | None
    outlined: bool


def compute_card_layout(spec: LabelSpec) -> CardLayout:
    blocks: list[TextBlock] = []
    fields = spec.fields
    if fields.primary:
        blocks.append(TextBlock(fields.primary, "primary"))
    if fields.secondary:
        blocks.append(TextBlock(fields.secondary, "secondary"))
    if fields.tertiary:
        blocks.append(TextBlock(fields.tertiary, "tertiary"))

    qr_shown = include_qr(spec)
    # The caption is the MPN-under-the-QR rule specifically — it never
    # appears as filler when there is no QR to caption, and it never
    # substitutes for `primary`/`secondary` elsewhere on the card.
    if qr_shown and fields.caption:
        blocks.append(TextBlock(fields.caption, "caption"))

    return CardLayout(
        width_mm=spec.width_mm,
        height_mm=spec.height_mm,
        text=tuple(blocks),
        qr_payload=spec.qr_payload if qr_shown else None,
        outlined=spec.outlined,
    )


#: Font size as a fraction of the card's shorter side, per text role. Bigger
#: than the *cap-height* fractions PLAN.md's legibility numbers use, because
#: `ImageFont.load_default(size=N)` sizes the whole glyph box, not the cap
#: height alone — the point is relative hierarchy (primary reads largest),
#: not a claim to have hit an exact arm's-length legibility target with a
#: bitmap font and no chosen printer yet.
_ROLE_SIZE_FRACTION = {"primary": 0.20, "secondary": 0.13, "tertiary": 0.10, "caption": 0.10}


def _font_for_role(
    role: str, short_side_px: int
) -> tuple[ImageFont.ImageFont | ImageFont.FreeTypeFont, int]:
    """The font, plus the size that chose it — returned alongside rather than
    read back off the font object, since `load_default`'s declared return
    type (`FreeTypeFont | ImageFont`) only guarantees a `.size` attribute on
    one branch of that union."""
    size = max(round(short_side_px * _ROLE_SIZE_FRACTION[role]), 8)
    return ImageFont.load_default(size=size), size


def _render_qr_image(payload: str, side_px: int) -> Image.Image:
    qr = segno.make(payload, error="m")
    modules = qr.symbol_size(border=1)[0]
    scale = max(side_px // modules, 1)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=1)
    buf.seek(0)
    return Image.open(buf).convert("RGB").resize((side_px, side_px), Image.Resampling.NEAREST)


def render_card_image(spec: LabelSpec) -> Image.Image:
    """`LabelSpec` -> a `PIL.Image` at exactly `mm_to_px(width/height, dpi)`.

    The only function in this module that is not pure data-in data-out —
    everything it draws was already decided by `compute_card_layout`, so a
    rendering bug here is a drawing bug, never a content bug.
    """
    width_px = mm_to_px(spec.width_mm, spec.dpi)
    height_px = mm_to_px(spec.height_mm, spec.dpi)
    short_side_px = min(width_px, height_px)
    pad_px = max(round(short_side_px * 0.06), 2)

    image = Image.new("RGB", (width_px, height_px), "white")
    draw = ImageDraw.Draw(image)

    layout = compute_card_layout(spec)
    if layout.outlined:
        border_px = max(round(spec.dpi / 150), 1)
        draw.rectangle((0, 0, width_px - 1, height_px - 1), outline="black", width=border_px)

    qr_side_px = 0
    if layout.qr_payload is not None:
        qr_side_px = short_side_px - 2 * pad_px
        qr_image = _render_qr_image(layout.qr_payload, qr_side_px)
        image.paste(qr_image, (width_px - pad_px - qr_side_px, pad_px))

    text_x = pad_px
    text_y = pad_px
    for block in layout.text:
        font, size = _font_for_role(block.role, short_side_px)
        draw.text((text_x, text_y), block.text, fill="black", font=font)
        text_y += size + max(round(size * 0.3), 1)

    return image
