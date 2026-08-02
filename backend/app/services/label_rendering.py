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

#: The QR's own floor, without the card-margin headroom `MIN_QR_DIMENSION_MM`
#: adds: PLAN.md's "roughly 13 mm square including its 4-module quiet zone" for a
#: version 2-3 symbol at ECC-M. Two numbers doing different jobs — the first
#: decides whether a card gets a QR at all, this one how far the QR may be
#: squeezed to leave room for text beside it.
QR_MIN_SCANNABLE_MM = 13.0

#: How much of a card's width is kept for text when the QR would otherwise take
#: all of it. Not a layout preference: below this the primary label and the short
#: id cannot both be read, and a card whose only content is a QR fails the one
#: job the printed text has.
_TEXT_WIDTH_SHARE = 0.45


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


def qr_side_for(width_px: int, short_side_px: int, pad_px: int, dpi: int) -> int:
    """How big the QR may be, given that text has to fit beside it.

    Its natural size is the card's height less padding, which is right for a
    landscape card and catastrophic for a nearly square one: a 41.5 x 42 mm
    Gridfinity face leaves **zero** px of width for text, so every block renders
    empty and the card carries a code and nothing a person can read. That is the
    one job PLAN.md gives the printed text — the zero-dependency fallback when a
    manual search is the only tool at hand.

    So the QR gives way, down to the ~13 mm it needs to stay scannable at this
    payload length (`MIN_QR_DIMENSION_MM` is that plus card-margin headroom,
    which is why the floor is the smaller number). Below that there is no honest
    trade left and it keeps its size: a scannable code with no caption beats a
    caption beside a code that will not read.

    Separated out because the alternative is asserting on pixels, and a QR's
    quiet zone means its ink does not start at its edge — a test measuring the
    rendered image measures the wrong thing and passes for the wrong reason.
    """
    natural_px = short_side_px - 2 * pad_px
    room_for_text_px = round((width_px - 2 * pad_px) * _TEXT_WIDTH_SHARE)
    floor_px = mm_to_px(QR_MIN_SCANNABLE_MM, dpi)
    return min(max(width_px - 2 * pad_px - room_for_text_px, floor_px), natural_px)


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
        # A square QR of the card's full height eats the whole width of a card
        # that is nearly square — a 41.5 x 42 mm Gridfinity face leaves *zero*
        # px for text, so every block rendered empty and the card carried a code
        # and no human-readable identity at all. That is the one job PLAN.md
        # gives the printed text: the zero-dependency fallback when a manual
        # search is the only tool at hand.
        #
        # So the QR gives way, but only down to the ~13 mm it needs to stay
        # scannable at this payload length. `MIN_QR_DIMENSION_MM` is that plus
        # card-margin headroom, which is why the floor here is the smaller
        # number. Below it there is no honest trade left and the QR keeps its
        # size — a scannable code with no caption beats a caption beside a code
        # that will not read.
        qr_side_px = qr_side_for(width_px, short_side_px, pad_px, spec.dpi)
        qr_image = _render_qr_image(layout.qr_payload, qr_side_px)
        image.paste(qr_image, (width_px - pad_px - qr_side_px, pad_px))

    # **The text has to be bounded on the right.** Nothing upstream knows
    # the pixel geometry: `compute_card_layout` decides *what* to print, and a
    # `label_path` is as long as the tree is deep. Drawn unbounded at PLAN.md's own
    # 40x18 mm card, a five-level path ran straight through the QR's data and
    # timing patterns — so the code would not scan, on a card that has already
    # been printed and put in a drawer front — and then PIL clipped the rest at
    # the image edge, mid-word and with nothing to say it had been cut.
    #
    # Only the horizontal bound is enforced. A vertical one would be unreachable
    # dead code: `compute_card_layout` emits at most four blocks, whose sizes and
    # gaps come to about 0.69 of the card's *shorter* side, so they cannot reach
    # the bottom of any card this renders. Adding a fifth role is the moment to
    # revisit that, and there is no guard here pretending to have handled it.
    text_right_px = (width_px - pad_px - qr_side_px - pad_px) if qr_side_px else (width_px - pad_px)
    text_x = pad_px
    text_y = pad_px
    available_px = max(text_right_px - text_x, 0)
    for block in layout.text:
        font, size, drawn = _fitted(draw, block, short_side_px, available_px)
        if drawn:
            draw.text((text_x, text_y), drawn, fill="black", font=font)
        # The row is advanced either way, so a dropped block leaves its gap
        # rather than sliding the ones below it into a different arrangement
        # than the layout described.
        text_y += size + max(round(size * 0.3), 1)

    return image


#: Roles that must be printed whole or not at all.
#:
#: A `short_id` is 7 data symbols **plus a mod-37 check symbol**, and the check
#: symbol is the last character — so right-truncating it does not merely shorten
#: the code, it removes the one character that makes a mistyped code detectable
#: and leaves something that still looks like an id. `CLAUDE.md` treats the code
#: as one indivisible unit; a partial one is worse than none, because a person
#: will type it in.
#:
#: The justification for truncating at all — "losing the tail costs the leaf
#: name, which is printed on its own line above" — is true of a `label_path` and
#: false of everything here.
_INDIVISIBLE_ROLES = frozenset({"secondary", "caption"})

#: The smallest a shrunk-to-fit block may get. A `short_id` is read off a drawer
#: front at arm's length and typed in; below this it is not doing that job, and
#: printing it anyway would look like diligence rather than be it.
_MIN_LEGIBLE_PX = 8


def _fitted(
    draw: ImageDraw.ImageDraw,
    block: TextBlock,
    short_side_px: int,
    max_width_px: float,
) -> tuple[ImageFont.ImageFont | ImageFont.FreeTypeFont, int, str]:
    """The font, its size, and the text to draw for one block.

    An indivisible block is **shrunk to fit rather than dropped**. Refusing to
    truncate a `short_id` was right and refusing to print it at all was not: the
    font size scales with the card's *shorter* side while the room beside the QR
    scales with its *width*, so on any card that is not distinctly landscape the
    code simply vanished — a 68 x 40 mm drawer front came out as a label, a path,
    a QR and a blank line, with the bottom half of the card empty. PLAN.md
    specifies the code as part of the drawer card, and `_TEXT_WIDTH_SHARE`'s own
    comment promises the label *and* the code can both be read.

    Shrinking stops at `_MIN_LEGIBLE_PX`, below which a code somebody has to read
    at arm's length is not worth the ink; if it still will not fit, it is dropped
    rather than printed at a size that only looks like diligence.
    """
    font, size = _font_for_role(block.role, short_side_px)
    if block.role not in _INDIVISIBLE_ROLES:
        return font, size, _ellipsised(draw, block.text, font, max_width_px)

    while size > _MIN_LEGIBLE_PX and draw.textlength(block.text, font=font) > max_width_px:
        size -= 1
        font = ImageFont.load_default(size=size)
    if draw.textlength(block.text, font=font) > max_width_px:
        return font, size, ""
    return font, size, block.text


def _ellipsised(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    max_width_px: float,
) -> str:
    """`text`, shortened with a trailing ellipsis until it fits `max_width_px`.

    Truncating from the right keeps the start, which for a `label_path` is the
    room and the cabinet — the part that tells somebody holding the card which
    wall to walk to. Losing the tail costs the leaf name, which is also printed
    on its own line above.
    """
    if max_width_px <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width_px:
        return text
    ellipsis = "…"
    if draw.textlength(ellipsis, font=font) > max_width_px:
        return ""
    kept = text
    while kept and draw.textlength(kept + ellipsis, font=font) > max_width_px:
        kept = kept[:-1]
    return kept + ellipsis
