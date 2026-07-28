"""Where a rendered card image ends up. Two backends, both hardware-free.

`LabelBackend` is deliberately the same shape `docs/PLAN.md` sketches for
every backend, real or not: a `capabilities` snapshot plus `print(image,
spec)`. That is the whole abstraction, and it is what matters more than
either implementation below — a later `ZplBackend` for a Zebra printer would
have to satisfy the identical Protocol with no change to
`app.services.labels.render_sheet`, which is the only caller either backend
has.

`PdfSheetBackend` does not run a general bin-packer. The caller already knows
the true physical grid — "sheets are laid out row-major in the same grid as
the physical drawers" is the whole reason a partial reprint (`slot_ids`) can
land on an already-cut sheet — so this backend only ever places a card at
`grid_row * pitch, grid_col * pitch` on a fixed-pitch page. A packer that
repositioned cards by "next free space" would defeat that alignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from app.services.label_rendering import LabelSpec


@dataclass(frozen=True)
class LabelBackendCapabilities:
    dpi: int
    #: Supported card widths in mm, or empty when the backend takes whatever
    #: geometry it is handed. Empty for both backends here: neither is a
    #: die-cut roll printer with a fixed set of stock widths.
    widths_mm: tuple[float, ...]
    min_width_mm: float
    #: Whether the backend can cut the medium itself. Neither can — cardstock
    #: sheets are cut by hand, and `FileBackend` writes a still image.
    cut: bool


@dataclass(frozen=True)
class PrintResult:
    output_path: Path
    width_mm: float
    height_mm: float


class LabelBackend(Protocol):
    capabilities: LabelBackendCapabilities

    def print(self, image: Image.Image, spec: LabelSpec) -> PrintResult: ...

    def finalize(self) -> Path:
        """Flush whatever `print()` accumulated to disk and return its path.
        A no-op for a backend with nothing to batch."""
        ...


class FileBackend:
    """PNG to disk — testing, and `docs/PLAN.md`'s "print later": a card with
    no thermal or laser backend built for it yet still lands somewhere real,
    one file per card, in the order `print()` was called."""

    def __init__(self, output_dir: Path, *, dpi: int = 300) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.capabilities = LabelBackendCapabilities(
            dpi=dpi, widths_mm=(), min_width_mm=0.0, cut=False
        )
        self._count = 0

    def print(self, image: Image.Image, spec: LabelSpec) -> PrintResult:
        self._count += 1
        path = self.output_dir / f"card-{self._count:03d}.png"
        image.save(path, format="PNG", dpi=(spec.dpi, spec.dpi))
        return PrintResult(output_path=path, width_mm=spec.width_mm, height_mm=spec.height_mm)

    def finalize(self) -> Path:
        return self.output_dir


class PdfSheetBackend:
    """Multi-up PDF for a plain cardstock sheet.

    Cards arrive already in row-major reading order (the caller's
    responsibility, not this class's), and each is placed at its own
    `grid_row`/`grid_col` times a **fixed** cell pitch — the base 1x1 cell
    size, supplied at construction, never the individual card's own
    (possibly span-scaled) size. That fixed pitch is what makes a one-card
    reprint line up with a sheet that was already cut: the hole where a slot's
    card used to be is always at `col * pitch`, regardless of which cards are
    present in this particular run.

    Pagination is single-axis: a grid taller than one page wraps onto
    additional pages of full width, in row-block order. A grid *wider* than
    one page is out of scope for Phase 1 — no printer is chosen yet, and every
    worked example in `docs/PLAN.md` (44-drawer cabinets, Raaco strips) fits
    comfortably within an A4/Letter width at any sane card size.
    """

    #: A4. No printer is chosen yet (`docs/PLAN.md` commits to none), so this
    #: is a constant rather than a parameter until a real purchase forces the
    #: question; Letter differs by only a few mm and the margin math does not
    #: depend on which one it ends up being.
    PAGE_WIDTH_MM = 210.0
    PAGE_HEIGHT_MM = 297.0
    MARGIN_MM = 10.0
    #: Space between adjacent cells so two cards' crop marks never touch.
    GUTTER_MM = 3.0
    #: Length of one arm of a hairline crop mark, centred on each card corner.
    CROP_MARK_MM = 3.0

    def __init__(
        self,
        output_path: Path,
        *,
        cell_width_mm: float,
        cell_height_mm: float,
        dpi: int = 300,
    ) -> None:
        self.output_path = output_path
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.cell_width_mm = cell_width_mm
        self.cell_height_mm = cell_height_mm
        self.capabilities = LabelBackendCapabilities(
            dpi=dpi, widths_mm=(), min_width_mm=0.0, cut=True
        )

        usable_height = self.PAGE_HEIGHT_MM - 2 * self.MARGIN_MM
        pitch = cell_height_mm + self.GUTTER_MM
        # At least one row per page even if a single (spanning) card's height
        # would not otherwise fit — a page that fits nothing is not a
        # graceful degradation, it is a silently empty sheet.
        self._rows_per_page = max(1, int((usable_height + self.GUTTER_MM) // pitch))

        self._canvas = canvas.Canvas(
            str(output_path), pagesize=(self.PAGE_WIDTH_MM * mm, self.PAGE_HEIGHT_MM * mm)
        )
        self._current_block: int | None = None

    def print(self, image: Image.Image, spec: LabelSpec) -> PrintResult:
        block = spec.grid_row // self._rows_per_page
        if self._current_block is None:
            self._current_block = block
        elif block != self._current_block:
            # Cards arrive row-major, so `block` is monotonically
            # non-decreasing — this is a strict advance, never a jump back,
            # which is the only page order `canvas.showPage()` supports.
            self._canvas.showPage()
            self._current_block = block

        row_in_page = spec.grid_row % self._rows_per_page
        x_mm = self.MARGIN_MM + spec.grid_col * (self.cell_width_mm + self.GUTTER_MM)
        y_mm = (
            self.PAGE_HEIGHT_MM
            - self.MARGIN_MM
            - (row_in_page + 1) * self.cell_height_mm
            - row_in_page * self.GUTTER_MM
        )
        self._draw_card(image, x_mm, y_mm, spec.width_mm, spec.height_mm, outlined=spec.outlined)
        return PrintResult(
            output_path=self.output_path, width_mm=spec.width_mm, height_mm=spec.height_mm
        )

    def _draw_card(
        self,
        image: Image.Image,
        x_mm: float,
        y_mm: float,
        w_mm: float,
        h_mm: float,
        *,
        outlined: bool,
    ) -> None:
        c = self._canvas
        c.drawImage(ImageReader(image), x_mm * mm, y_mm * mm, width=w_mm * mm, height=h_mm * mm)
        if outlined:
            # A spanning card's own render already has an internal border
            # (`render_card_image`); this one is drawn directly on the sheet
            # so it survives even if the embedded image is later replaced.
            c.setLineWidth(0.5)
            c.rect(x_mm * mm, y_mm * mm, w_mm * mm, h_mm * mm, stroke=1, fill=0)
        self._draw_crop_marks(x_mm, y_mm, w_mm, h_mm)

    def _draw_crop_marks(self, x_mm: float, y_mm: float, w_mm: float, h_mm: float) -> None:
        c = self._canvas
        c.setLineWidth(0.25)
        length = self.CROP_MARK_MM
        for corner_x, corner_y in (
            (x_mm, y_mm),
            (x_mm + w_mm, y_mm),
            (x_mm, y_mm + h_mm),
            (x_mm + w_mm, y_mm + h_mm),
        ):
            c.line((corner_x - length) * mm, corner_y * mm, (corner_x + length) * mm, corner_y * mm)
            c.line(corner_x * mm, (corner_y - length) * mm, corner_x * mm, (corner_y + length) * mm)

    def finalize(self) -> Path:
        self._canvas.save()
        return self.output_path
