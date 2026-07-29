"""Minimal object factories for tests.

Deliberately thin — just enough to satisfy NOT NULL columns so a test can say
what it is actually about. Anything cleverer becomes a second, untested model
layer.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Manufacturer, Packaging, Part, PartCategory, PartKind
from app.models.enums import AllocationState, LedgerKind, LedgerSource
from app.models.projects import BomLine, Project, ProjectBuild, StockAllocation
from app.models.stock import StockLedger, StockLot
from app.models.storage import ContainerType, Location
from app.scripts import seed_demo
from app.services.capacity import get_inbox_location
from app.services.requirements.vocabulary import (
    CategoryVocab,
    ChoiceVocab,
    TemplateVocab,
    Vocabulary,
)
from app.services.scanning.codes import normalize_mpn


def component_kind(db: Session) -> PartKind:
    """The 'component' row seeded by the initial migration."""
    return db.execute(select(PartKind).where(PartKind.slug == "component")).scalar_one()


def inbox_location(db: Session) -> Location:
    """The permanent staging row seeded by the capacity/assignment migration.

    Delegates to the service rather than re-deriving the lookup: `is_staging` is
    no longer unique — ADR 0004 gives every project a staging box carrying the
    same flag — and a test asserting on a *different* definition of "the INBOX"
    than the assignment ladder uses would pass while the ladder was broken.
    """
    return get_inbox_location(db)


def make_part(db: Session, name: str = "Test part", **kwargs: object) -> Part:
    # `mpn_norm` is derived, and deriving it here rather than leaving it NULL is
    # what makes the resolver's bare-MPN step testable through the same door the
    # real write path will use. A test that wants the column empty or wrong
    # passes it explicitly.
    mpn = kwargs.get("mpn")
    if isinstance(mpn, str) and "mpn_norm" not in kwargs:
        kwargs["mpn_norm"] = normalize_mpn(mpn)
    part = Part(name=name, part_kind_id=component_kind(db).id, **kwargs)
    db.add(part)
    db.flush()
    return part


def make_location(db: Session, name: str = "Test bin", **kwargs: object) -> Location:
    location = Location(name=name, **kwargs)
    db.add(location)
    db.flush()
    return location


def make_container_type(
    db: Session, slug: str = "test-container", **kwargs: object
) -> ContainerType:
    kwargs.setdefault("display_name", slug)
    container_type = ContainerType(slug=slug, **kwargs)
    db.add(container_type)
    db.flush()
    return container_type


def make_packaging(db: Session, code: str = "test-packaging", **kwargs: object) -> Packaging:
    kwargs.setdefault("display_name", code)
    packaging = Packaging(code=code, **kwargs)
    db.add(packaging)
    db.flush()
    return packaging


def make_manufacturer(
    db: Session, name: str = "Test Semiconductor", **kwargs: object
) -> Manufacturer:
    # `name_norm` is NOT NULL and the real write path casefolds and strips
    # punctuation; casefolding alone is enough for a test to be unambiguous.
    kwargs.setdefault("name_norm", name.casefold())
    manufacturer = Manufacturer(name=name, **kwargs)
    db.add(manufacturer)
    db.flush()
    return manufacturer


def make_category(db: Session, name: str = "Test category", **kwargs: object) -> PartCategory:
    kwargs.setdefault("slug", name.lower().replace(" ", "-"))
    category = PartCategory(name=name, **kwargs)
    db.add(category)
    db.flush()
    return category


def make_lot(db: Session, part: Part, location: Location, qty_milli: int = 0) -> StockLot:
    lot = StockLot(part_id=part.id, location_id=location.id, qty_milli_cached=qty_milli)
    db.add(lot)
    db.flush()
    return lot


def post(
    db: Session,
    lot: StockLot,
    delta_milli: int,
    kind: LedgerKind = LedgerKind.ADJUST,
    **kwargs: object,
) -> StockLedger:
    """Append a ledger row and move the cached balance with it.

    This mirrors what a real write path must do — ledger row and cache updated
    together in one transaction. Tests that deliberately break the pairing do
    so explicitly, so drift detection has something to detect.
    """
    lot.qty_milli_cached += delta_milli
    row = StockLedger(
        lot_id=lot.id,
        part_id=lot.part_id,
        kind=kind,
        delta_milli=delta_milli,
        qty_after_milli=lot.qty_milli_cached,
        source=LedgerSource.MANUAL,
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def make_project(db: Session, name: str = "Test board", **kwargs: object) -> Project:
    project = Project(name=name, **kwargs)
    db.add(project)
    db.flush()
    return project


def make_build(db: Session, project: Project, build_no: int = 1, **kwargs: object) -> ProjectBuild:
    build = ProjectBuild(project_id=project.id, build_no=build_no, **kwargs)
    db.add(build)
    db.flush()
    return build


def make_bom_line(
    db: Session, project: Project, qty_per_assembly_milli: int = 1_000, **kwargs: object
) -> BomLine:
    """A BOM line. Note `part_id` is *not* defaulted: an unmatched line is the
    normal case an import produces, so a test has to opt in to a matched one."""
    line = BomLine(project_id=project.id, qty_per_assembly_milli=qty_per_assembly_milli, **kwargs)
    db.add(line)
    db.flush()
    return line


def make_allocation(
    db: Session,
    build: ProjectBuild,
    part: Part,
    qty_milli: int,
    state: AllocationState = AllocationState.PLANNED,
    lot: StockLot | None = None,
    **kwargs: object,
) -> StockAllocation:
    """An allocation row, written **without** touching
    `stock_lots.qty_reserved_milli_cached`.

    That omission is the point: the cache is derived, so a test proves the
    rebuild reconstructs it from allocations alone. A factory that maintained
    the counter here would make every such test tautological.
    """
    allocation = StockAllocation(
        build_id=build.id,
        part_id=part.id,
        lot_id=None if lot is None else lot.id,
        qty_milli=qty_milli,
        state=state,
        **kwargs,
    )
    db.add(allocation)
    db.flush()
    return allocation


def seed_vocabulary() -> Vocabulary:
    """The requirement-parsing vocabulary of a freshly seeded install, with no database.

    Built from `app.scripts.seed_demo`'s own `TEMPLATES` and `CATEGORIES` tuples —
    the *same* constants the seed script writes rows from. That is what lets
    `tests/unit/test_requirements.py` be a real unit test (no session, no
    migrations, one object) without its vocabulary drifting away from the one a
    real install has: `tests/integration/test_requirements.py` seeds a database,
    calls `load_vocabulary`, and asserts the two agree.

    Nothing here invents a template or a spelling. A test that needs a facet the
    seed does not have builds its own `Vocabulary` inline, so the addition is
    visible in the test rather than hidden in this helper.
    """
    templates = tuple(
        TemplateVocab(
            name=spec.name,
            display_name=spec.display_name,
            value_type=spec.value_type,
            base_unit=spec.base_unit,
            applies_to_category=spec.applies_to_category,
            plausible_min=spec.plausible_min,
            plausible_max=spec.plausible_max,
            choices=tuple(
                ChoiceVocab(key=choice.key, label=choice.label, aliases=choice.aliases)
                for choice in spec.choices
            ),
        )
        for spec in seed_demo.TEMPLATES
    )
    categories = tuple(
        CategoryVocab(slug=slug, name=name) for slug, name, _parent in seed_demo.CATEGORIES
    )
    return Vocabulary(templates=templates, categories=categories)
