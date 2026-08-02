"""Label sheets: resolving current data, placing each card on the physical
grid, and handing rendered images to a backend.

Three invariants carried straight from `docs/PLAN.md`:

* **The server always re-fetches** name, path and short id at print time and
  never accepts them from the caller. `resolve_label_fields` takes only a
  `Location` freshly loaded from the session — there is no text field on
  `LabelSheetRequest` (see `app/api/routes/labels.py`) a stale client value
  could even occupy, so nothing here can "trust" caller-supplied text; the
  guarantee is structural, not a runtime check that could be forgotten.
* **Sheets are laid out row-major in the same grid as the physical
  drawers.** `render_sheet` reads each slot's own `row_idx`/`col_idx` and
  never renumbers them, so a `slot_ids`-filtered reprint lands exactly where
  the original card did — the whole point of "positioned on a partly-used
  sheet".
* **No contents or counts, ever.** Nothing in this module reads
  `stock_lots`; a drawer or cabinet card shows identity and position only.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import EntityType, LabelBackendKind, LabelTemplate
from app.models.layout_authoring import LabelPrint, LabelSheetJob
from app.models.storage import ContainerType, Location
from app.models.types import utcnow
from app.services import provisioning, shortid
from app.services.label_backends import FileBackend, LabelBackend, PdfSheetBackend
from app.services.label_rendering import LabelFields, LabelSpec, include_qr, render_card_image

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LabelError(ValueError):
    """A sheet request that cannot mean what it says. Carries a `reason` the
    route maps to a status code, matching `app.services.layout_authoring.
    LayoutError` and `app.services.provisioning.ProvisioningError`."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Card geometry: docs/PLAN.md's worked example, pinned as constants
# ---------------------------------------------------------------------------

#: A 46x22 mm drawer front becomes a 40x18 mm card. Width and height lose
#: different amounts because the lip only grips two opposite edges of the
#: label slot (the long edges, on a wide-short card like Raaco's), not a
#: uniform margin on all four — there is no single "the margin" to name, so
#: both numbers are pinned to PLAN.md's one worked example rather than
#: derived from a general formula the design never states.
LIP_MARGIN_WIDTH_MM = 6.0
LIP_MARGIN_HEIGHT_MM = 4.0


def card_size_mm(container_type: ContainerType | None) -> tuple[float, float]:
    """The base 1x1 cell size a sheet is built from: `front_width_mm` /
    `front_height_mm` minus the lip margin.

    Raises rather than guessing when the type declares neither — a card
    cannot be sized from nothing, and a made-up default would print at
    whatever size happened to be convenient rather than what the drawer
    actually is.
    """
    if (
        container_type is None
        or container_type.front_width_mm is None
        or container_type.front_height_mm is None
    ):
        named = "this container" if container_type is None else f'"{container_type.display_name}"'
        raise LabelError(
            f"{named} has no drawer-front dimensions recorded, so a card cannot be "
            "sized: measure the face a label sits on and set front width and height "
            "on the container type. Guessing would print at whatever size happened "
            "to be convenient rather than at what the drawer is.",
            reason="missing_front_dimensions",
        )
    width = container_type.front_width_mm - LIP_MARGIN_WIDTH_MM
    height = container_type.front_height_mm - LIP_MARGIN_HEIGHT_MM
    if width <= 0 or height <= 0:
        raise LabelError(
            f"front dimensions {container_type.front_width_mm}x"
            f"{container_type.front_height_mm} mm are too small for the lip margin",
            reason="front_too_small",
        )
    return width, height


def _container_type_of(session: Session, location: Location) -> ContainerType | None:
    if location.container_type_id is None:
        return None
    return session.get(ContainerType, location.container_type_id)


def _slot_children(session: Session, root: Location) -> list[Location]:
    """`root`'s children that occupy a cell of its grid, in reading order —
    the same query shape `app.api.routes.locations._layout_read` uses, so the
    editor, the provisioning walk and the label sheet all agree on what "this
    cabinet's slots" means."""
    return list(
        session.execute(
            select(Location)
            .where(
                Location.parent_id == root.id,
                Location.row_idx.isnot(None),
                Location.col_idx.isnot(None),
            )
            .order_by(Location.sort_order, Location.id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Content: always derived, never trusted from a request
# ---------------------------------------------------------------------------


def resolve_label_fields(
    session: Session, target: Location, root: Location, template: LabelTemplate
) -> tuple[LabelFields, str]:
    """The card's text plus its QR payload, read fresh from `target` and
    `root` — never from anything a caller passed in, because nothing a caller
    can pass in describes a card's content at all (see the module docstring).

    Mints a `short_id` for `target` if it does not have one yet, exactly as
    `app.services.provisioning.ndef_url_for` does for a tag write: printing a
    physical card presupposes the location is about to have a printed
    identity, generated-grid-cell or not.
    """
    payload = provisioning.ndef_url_for(session, target)
    short_id = provisioning.printed_short_id(session, target.id)
    display_id = shortid.format_display(short_id) if short_id else ""

    if template == LabelTemplate.DRAWER_CARD:
        fields = LabelFields(
            primary=target.slot_label or target.name,
            secondary=display_id,
            tertiary=root.name,
        )
    elif template == LabelTemplate.CABINET_CARD:
        fields = LabelFields(
            primary=target.name,
            secondary=display_id,
            tertiary=target.label_path,
        )
    else:
        raise LabelError(
            f"{template} has no location-based rendering", reason="unsupported_template"
        )
    return fields, payload


# ---------------------------------------------------------------------------
# Placement: one card, one grid cell, in physical reading order
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CardPlacement:
    """One rendered card's position and identity — JSON-safe, and everything
    `GET /api/labels/sheets/{id}` needs to answer "what got printed and
    where" with no re-render."""

    location_id: int
    row: int
    col: int
    row_span: int
    col_span: int
    slot_label: str | None
    name: str
    short_id: str | None
    qr_included: bool
    width_mm: float
    height_mm: float


def placements_to_json(placements: Sequence[CardPlacement]) -> str:
    return json.dumps([asdict(p) for p in placements])


def placements_from_json(raw: str) -> list[CardPlacement]:
    return [CardPlacement(**item) for item in json.loads(raw)]


def _cell_position(location: Location) -> tuple[int, int]:
    """Narrows the ORM's `int | None` `row_idx`/`col_idx` to `int` for a
    location already known to be a slot — mirrors
    `app.api.routes.locations._slot_state_read`'s identical guard."""
    row_idx, col_idx = location.row_idx, location.col_idx
    if row_idx is None or col_idx is None:
        # pragma: no cover - callers only ever pass a `_slot_children()` row
        raise ValueError(f"location {location.id} is not a slot")
    return row_idx, col_idx


def _cells_for_request(
    session: Session, root: Location, template: LabelTemplate, slot_ids: Sequence[int] | None
) -> list[tuple[Location, int, int, int, int]]:
    """`(location, row, col, row_span, col_span)` for every card this request
    draws, in row-major reading order — regardless of what order `slot_ids`
    named them in, since the sheet's reading order is a property of the
    physical grid, not of the request body.
    """
    if template == LabelTemplate.CABINET_CARD:
        if slot_ids:
            raise LabelError(
                "slot_ids only applies to drawer_card; a cabinet card has no "
                "grid of its own to filter",
                reason="slot_ids_not_applicable",
            )
        return [(root, 0, 0, 1, 1)]

    if template != LabelTemplate.DRAWER_CARD:
        raise LabelError(f"{template} is not sheet-printable here", reason="unsupported_template")

    children = _slot_children(session, root)
    if not children:
        raise LabelError(f"{root.name!r} has no slotted children to print", reason="no_slots")

    if slot_ids is not None:
        wanted = set(slot_ids)
        missing = wanted - {child.id for child in children}
        if missing:
            raise LabelError(
                f"slot id(s) {sorted(missing)} are not slotted children of {root.name!r}",
                reason="unknown_slot_ids",
            )
        # Filtering `children` (already in reading order) rather than mapping
        # over `slot_ids` is what makes the result's order independent of the
        # order the caller happened to list them in.
        children = [child for child in children if child.id in wanted]

    return [(child, *_cell_position(child), child.row_span, child.col_span) for child in children]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _make_backend(
    kind: LabelBackendKind,
    *,
    output_dir: Path,
    dpi: int,
    cell_width_mm: float,
    cell_height_mm: float,
) -> LabelBackend:
    if kind == LabelBackendKind.FILE:
        return FileBackend(output_dir, dpi=dpi)
    if kind == LabelBackendKind.PDF_SHEET:
        return PdfSheetBackend(
            output_dir / "sheet.pdf",
            cell_width_mm=cell_width_mm,
            cell_height_mm=cell_height_mm,
            dpi=dpi,
        )
    raise LabelError(
        f"backend {kind!r} is not implemented", reason="unsupported_backend"
    )  # pragma: no cover


@dataclass(frozen=True)
class RenderedSheet:
    job: LabelSheetJob
    placements: list[CardPlacement]


def render_sheet(
    session: Session,
    *,
    root: Location,
    template: LabelTemplate,
    slot_ids: Sequence[int] | None,
    backend_kind: LabelBackendKind,
    dpi: int,
) -> RenderedSheet:
    """Render, print, and record one sheet job.

    Order matters: every card is rendered and handed to the backend *before*
    the `label_sheet_jobs` row is created, because that row's `item_count`
    and `placements_json` describe what actually got drawn — not what was
    requested, which could differ if a future extension made some cards
    skippable mid-run.
    """
    container_type = _container_type_of(session, root)
    base_width_mm, base_height_mm = card_size_mm(container_type)
    cells = _cells_for_request(session, root, template, slot_ids)

    output_dir = Path(get_settings().label_output_dir) / uuid4().hex
    backend = _make_backend(
        backend_kind,
        output_dir=output_dir,
        dpi=dpi,
        cell_width_mm=base_width_mm,
        cell_height_mm=base_height_mm,
    )

    placements: list[CardPlacement] = []
    print_rows: list[LabelPrint] = []
    now = utcnow()

    for target, row, col, row_span, col_span in cells:
        fields, payload = resolve_label_fields(session, target, root, template)
        width_mm = base_width_mm * col_span
        height_mm = base_height_mm * row_span
        spec = LabelSpec(
            template=template,
            width_mm=width_mm,
            height_mm=height_mm,
            dpi=dpi,
            fields=fields,
            qr_payload=payload,
            outlined=row_span > 1 or col_span > 1,
            grid_row=row,
            grid_col=col,
        )
        image = render_card_image(spec)
        backend.print(image, spec)

        placements.append(
            CardPlacement(
                location_id=target.id,
                row=row,
                col=col,
                row_span=row_span,
                col_span=col_span,
                slot_label=target.slot_label,
                name=target.name,
                short_id=provisioning.printed_short_id(session, target.id),
                qr_included=include_qr(spec),
                width_mm=width_mm,
                height_mm=height_mm,
            )
        )
        print_rows.append(
            LabelPrint(
                entity_type=EntityType.LOCATION,
                entity_pk=target.id,
                template=str(template),
                backend=str(backend_kind),
                dpi=dpi,
                width_mm=width_mm,
                height_mm=height_mm,
                succeeded=True,
            )
        )
        # Drives the "never printed" badge; a badge that only cleared on a
        # *successful* print would be wrong here too, since a rendering bug
        # would raise before this line and the whole job would not commit.
        target.last_printed_at = now

    output_path = backend.finalize()

    job = LabelSheetJob(
        template=str(template),
        backend=str(backend_kind),
        dpi=dpi,
        root_location_id=root.id,
        requested_slot_ids_json=json.dumps(sorted(slot_ids)) if slot_ids else None,
        card_width_mm=base_width_mm,
        card_height_mm=base_height_mm,
        item_count=len(placements),
        output_path=str(output_path),
        placements_json=placements_to_json(placements),
    )
    session.add(job)
    session.flush()

    for print_row in print_rows:
        print_row.job_ref = str(job.id)
        session.add(print_row)
    session.flush()

    return RenderedSheet(job=job, placements=placements)
