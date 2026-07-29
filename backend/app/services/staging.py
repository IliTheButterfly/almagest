"""The `locations` a project stages parts into, created lazily (ADR 0004).

Parts set aside for a project have **physically left the drawer**, so the only
honest way to record them is as stock at a different place — a real
`stock_ledger` move to a real location. That is what this module supplies: the
destination. It creates nothing else and touches no quantity; the movement
itself is `app.services.ledger`'s, and the allocation state is
`app.services.reservations`'.

The layout is three deep and is deliberately made of *ordinary* locations:

    PROJECTS                      is_staging, is_placeable = False
    └── Blinky v2                 ← the project's floating parts
        ├── Build 1 Assembly 1    ← committed to one specific unit
        └── Build 1 Assembly 2

Ordinary, because `locations` is already an adjacency list with no depth limit
and a working path cache, so "a project, or one of its assemblies" needs no new
table and no new columns — every existing bin screen, scan path and capacity
rule works on these unchanged. A parallel "project inventory" table would be a
second place quantity lives, which is the mistake PartKeepr made with
quantity-on-part and would need its own reconciliation forever.

Two flags carry the whole meaning:

* `is_placeable = False` — auto-assignment must never propose a project box as
  a *home* for incoming stock. `assignment.hard_filter_reasons` rejects a
  non-placeable location in both its strict and relaxed passes, so this is
  enough; no special case anywhere.
* `is_staging = True` — the same flag `INBOX` uses, so anything already written
  for staging applies. Note that `is_staging` alone therefore does **not** mean
  "spoken for": INBOX is staging and its stock is free. What excludes a project
  box from availability is its position in this subtree, which is why
  :func:`staging_subtree_prefix` exists and `reservations.available_by_part`
  filters on it rather than on the flag.

**Node identity is a `slot_label`, not a name.** `locations` already enforces
`UNIQUE(parent_id, slot_label)` for non-NULL labels, so keying the project node
on `P{project_id}` and an assembly node on `B{build_no}-A{assembly_no}` makes
lazy creation idempotent *in the database* rather than in a racing read. Names
are for humans and are deliberately not unique — two revisions of a board
legitimately share one (`Project.name`), so a name lookup would eventually
merge two projects' parts into one box.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.projects import Project, ProjectBuild
from app.models.stock import StockLot
from app.models.storage import Location
from app.services.tree import location_tree

#: The root every project box hangs under. A single fixed root rather than one
#: per project at top level, so "everything currently set aside for a project"
#: is one prefix query and the storage tree keeps one tidy branch.
STAGING_ROOT_NAME = "PROJECTS"

#: `slot_label` of the root. The root's parent is NULL, and SQL treats NULLs in
#: a unique index as distinct, so unlike its descendants the root is *not*
#: DB-guaranteed unique on `(parent_id, slot_label)` — which is why
#: :func:`staging_root` matches on `is_staging` too rather than on the label
#: alone. See that function for the bug that taught us the difference.
STAGING_ROOT_SLOT_LABEL = "PROJECTS"


class StagingError(ValueError):
    """A destination that cannot be interpreted. `reason` is machine-readable."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _new_node(name: str, *, parent_id: int | None, slot_label: str, sort_order: int) -> Location:
    """A staging location, with every flag that makes it one.

    `container_type_id` stays NULL on purpose: a project box has no measured
    geometry, so the capacity model resolves to `none` and occupancy reports it
    as "no defined capacity" rather than inventing a fill ratio for a cardboard
    tray nobody measured.
    """
    return Location(
        name=name,
        parent_id=parent_id,
        slot_label=slot_label,
        sort_order=sort_order,
        is_staging=True,
        # Explicit False, not NULL: NULL means "inherit from the container
        # type", and with no container type that resolves to placeable.
        is_placeable=False,
    )


def _child_by_slot_label(
    session: Session, parent_id: int | None, slot_label: str
) -> Location | None:
    return session.execute(
        select(Location).where(Location.parent_id == parent_id, Location.slot_label == slot_label)
    ).scalar_one_or_none()


def project_display_name(project: Project) -> str:
    """What goes on the shelf: "Blinky v2". Never used to *find* the node."""
    revision = (project.revision or "").strip()
    return f"{project.name} {revision}".strip() if revision else project.name


def _existing_staging_root(session: Session) -> Location | None:
    """The staging root, matched on **`is_staging` as well as the label**.

    Review found the label-only version breaking every staging read: `POST
    /api/locations` accepts `slot_label` freely, `UNIQUE(parent_id, slot_label)`
    does not bind when `parent_id` is NULL, so a user labelling a top-level
    shelf `PROJECTS` produced two rows and `scalar_one_or_none` raised
    `MultipleResultsFound` — 500s from `/shortages`, `/pick-list`, `/allocate`
    and `DELETE /api/projects/{id}` at once. Created *first*, that shelf was
    worse than a 500: it was silently **adopted** as the staging root, so its
    whole subtree stopped counting as available and `reserve` refused every lot
    in it. A build then reported a shortage of parts sitting on the shelf.

    `is_staging` is what makes the predicate safe, and it is safe because it is
    **not user-writable**: it appears on `LocationRead` but on neither
    `LocationCreate` nor the PATCH body, so only this module ever sets it. That
    is a stronger guarantee than a unique index, which would additionally have
    to reject a legitimately-labelled human shelf to hold.

    `order_by(id).limit(1)` is belt and braces rather than the guarantee: two
    rows here would mean someone wrote `is_staging` by hand, and answering
    deterministically beats 500ing on every read until they undo it.
    """
    return (
        session.execute(
            select(Location)
            .where(
                Location.parent_id.is_(None),
                Location.slot_label == STAGING_ROOT_SLOT_LABEL,
                Location.is_staging.is_(True),
            )
            .order_by(Location.id)
            .limit(1)
        )
        .scalars()
        .first()
    )


def staging_root(session: Session, *, create: bool = True) -> Location | None:
    """The `PROJECTS` root. `create=False` reads without materialising it.

    Every read path wants the second form: "is any of this part's stock spoken
    for by a project" must not create a location as a side effect of a GET.
    """
    existing = _existing_staging_root(session)
    if existing is not None or not create:
        return existing
    return location_tree(session).insert_and_index(
        _new_node(
            STAGING_ROOT_NAME,
            parent_id=None,
            slot_label=STAGING_ROOT_SLOT_LABEL,
            # Last among the real storage roots — this is bookkeeping furniture,
            # not somewhere anyone browses to find a part.
            sort_order=1000,
        )
    )


def project_staging_location(session: Session, project: Project) -> Location:
    """The project's own box, created on demand.

    Named for the project *and its revision* ("Blinky v2"), because that is what
    a human reads on the shelf, while identity comes from the `P{id}` slot
    label — see the module docstring.
    """
    root = staging_root(session)
    assert root is not None  # create=True never returns None
    label = f"P{project.id}"
    existing = _child_by_slot_label(session, root.id, label)
    if existing is not None:
        return existing
    return location_tree(session).insert_and_index(
        _new_node(
            project_display_name(project),
            parent_id=root.id,
            slot_label=label,
            sort_order=project.id,
        )
    )


def assembly_staging_location(
    session: Session, build: ProjectBuild, project: Project, assembly_no: int
) -> Location:
    """The box for one specific unit of one build, created on demand.

    Scoped to the build, not just to the assembly number, because assembly 2 of
    build 1 and assembly 2 of build 2 are two different physical boards — the
    whole reason `assembly_count` lives on `project_builds` and not on
    `projects`. Sharing one "Assembly 2" across iterations would put last
    month's parts and this month's in the same box and call it correct.
    """
    if assembly_no < 1 or assembly_no > build.assembly_count:
        # Refused rather than accepted-and-flagged, unlike an over-capacity
        # put-away: capacity being wrong still describes a real place, while
        # "assembly 7 of 3" names a unit that does not exist, so there is
        # nothing a later correction could attach the parts to.
        raise StagingError(
            f"build {build.id} makes {build.assembly_count} assemblies; {assembly_no} is not one",
            reason="unknown_assembly",
        )

    parent = project_staging_location(session, project)
    label = f"B{build.build_no}-A{assembly_no}"
    existing = _child_by_slot_label(session, parent.id, label)
    if existing is not None:
        return existing
    return location_tree(session).insert_and_index(
        _new_node(
            f"Build {build.build_no} Assembly {assembly_no}",
            parent_id=parent.id,
            slot_label=label,
            sort_order=assembly_no,
        )
    )


def destination_for(
    session: Session, build: ProjectBuild, project: Project, assembly_no: int | None
) -> Location:
    """Where a withdrawal for this build lands, creating what it needs.

    `assembly_no is None` means the project's floating parts — set aside for the
    project, not yet committed to a unit, which is exactly the "floating" state
    the requirement asks for.

    Records `project_builds.staging_location_id` on the way past. It is the
    project node in both branches: an assembly box is a child of it, so the
    pointer answers "where did this build's parts go" either way, and the box a
    user carries to the bench is the project's.
    """
    node = project_staging_location(session, project)
    build.staging_location_id = node.id
    if assembly_no is None:
        return node
    return assembly_staging_location(session, build, project, assembly_no)


def staging_subtree_prefix(session: Session) -> str | None:
    """`id_path` prefix of every project box, or None if none exists yet.

    The predicate that answers "is this stock spoken for by a project", as the
    left-anchored `LIKE` the `id_path` index serves. Returning None rather than
    a never-matching pattern lets a caller skip the join entirely on a database
    where nothing has ever been staged, which is most of them.
    """
    root = staging_root(session, create=False)
    return None if root is None else root.id_path


def staging_locations_of_project(session: Session, project: Project) -> list[Location]:
    """The project's box and every assembly box under it, deepest first.

    Deepest first because `locations.parent_id` is `ON DELETE RESTRICT` — a
    caller deleting these has to take the children before the parent, and
    getting that order from the query is cheaper than re-deriving it.
    """
    root = _existing_staging_root(session)
    if root is None:
        return []
    node = _child_by_slot_label(session, root.id, f"P{project.id}")
    if node is None:
        return []
    nodes = location_tree(session).subtree(node)
    return sorted(nodes, key=lambda location: (-location.depth, location.id))


def stock_in_staging(session: Session, project: Project) -> list[StockLot]:
    """Lots with parts still in this project's boxes.

    Zero-balance lots are excluded: an emptied lot is a historical record of a
    package that was there, not stock, and refusing to delete a project over one
    would make the refusal permanent for every project that ever built anything.
    """
    locations = staging_locations_of_project(session, project)
    if not locations:
        return []
    return list(
        session.execute(
            select(StockLot)
            .where(
                StockLot.location_id.in_([location.id for location in locations]),
                StockLot.qty_milli_cached != 0,
            )
            .order_by(StockLot.id)
        ).scalars()
    )
