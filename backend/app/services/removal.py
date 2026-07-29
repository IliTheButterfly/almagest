"""Removing a container: what can go, what has to stay, and why.

Iliana: *"I also noticed that I wasn't able to remove items in the workshop."*
There was no delete for a location anywhere, and writing one is not a one-liner,
because two of this system's load-bearing rules meet here and disagree:

* **The ledger is append-only and history is never deleted.** `stock_lots.
  location_id` and `stock_ledger.{from,to}_location_id` are all `ON DELETE
  RESTRICT`, nothing ever deletes a lot row, and `stock_ledger`'s UPDATE and
  DELETE are refused by trigger. A drawer that has *ever* held anything is
  therefore pinned to the database permanently.
* **Furniture is furniture.** An empty cell stamped out of a template is not
  data; it is a mistake someone made in the layout editor thirty seconds ago,
  and removing it must be ordinary and instant.

So there is no single answer, and pretending there is would either delete history
or leave a used drawer on screen forever. This module produces a **plan**, per
node, and says which of three things applies:

`delete`
    Nothing physical or historical names this row: no lot has ever sat in it, no
    ledger row moved stock through it, no label has been printed for it, no tag
    is stuck to it. The row goes. This is the common case.

`retire`
    Something names it. The row stays — with every ledger row that mentions it
    intact — and the container leaves the storage tree, its parent's slot canvas,
    the room plan and auto-assignment. Reversible: see `restore`.

*blocked*
    A lot with a non-zero balance is sitting in it, at this node or anywhere
    below. **Refused, and the refusal names what is inside**, because "constraint
    failed" is not an answer and silently relocating somebody's resistors is
    worse than an error. Nothing here ever moves stock: that is a movement, it
    belongs in the ledger, and it is the user's decision where to.

Two smaller decisions worth stating, because both are the difference between
this being usable and being a nuisance:

**A minted-but-unprinted `short_id` does not pin a row.** `instantiate` mints one
for every container root it stamps out, so treating a `short_id` as a physical
artifact would make almost nothing deletable. `locations.last_printed_at` and
`label_prints` are what record that a code reached the physical world; absent
both, the `object_ids` row is a database row and goes with the location.

**A bound NFC tag does pin a row.** The tag exists, in the workshop, with this
location's URL written on it — and that is exactly why retiring is the right
outcome rather than refusing: the tag still resolves, and
`/api/resolve/{short_id}` reports the container as removed. A tag that resolved
to nothing would read as a blank tag offering to be provisioned, which is a
different and wrong thing to say.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import Part
from app.models.enums import EntityType
from app.models.identity import ObjectId
from app.models.layout_authoring import LabelPrint
from app.models.stock import StockLedger, StockLot
from app.models.storage import Location, LocationTag
from app.services import room_plan
from app.services.tree import location_tree

#: How many examples a refusal or a plan spells out before summarising. Long
#: enough to be concrete about what is in the way, short enough that the answer
#: is still a sentence a person reads rather than a report they skim.
_MAX_NAMED = 5


class RemovalRefused(Exception):
    """The subtree holds stock, or holds children the caller did not ask to remove.

    Carries the machine-readable `reason` plus the human list of what is in the
    way, so the route turns it into a 409 without re-deriving anything.
    """

    def __init__(self, reason: str, message: str, blockers: tuple[Blocker, ...]) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.blockers = blockers


@dataclass(frozen=True)
class Blocker:
    """One reason the removal cannot proceed, attached to the node it is about."""

    #: `holds_stock` or `has_children`.
    reason: str
    location_id: int
    label: str
    label_path: str
    #: Prose naming the actual contents — the part and how much of it. This is
    #: the whole point: a refusal that does not say what is inside is useless.
    detail: str


@dataclass(frozen=True)
class NodePlan:
    """What will happen to one node of the subtree."""

    location_id: int
    label: str
    label_path: str
    #: `delete` or `retire`.
    action: str
    #: Why it cannot simply be deleted — `has_lots`, `in_ledger`, `printed`,
    #: `bound_tag`, `pinned_by_child`. Empty for a `delete`, and never empty for
    #: a `retire`: retiring without a reason would be an unexplained tombstone.
    pins: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemovalPlan:
    """A removal that **can** go ahead, node by node.

    There is no `blockers` field and no `removable` flag: a plan that cannot be
    carried out is not a plan, it is a `RemovalRefused`. That way there is no
    third state in which a caller might apply a plan it should have checked first.
    """

    root_id: int
    #: Deepest-first, which is both the order the deletes must reach SQL in and
    #: the order a human reads a list of consequences.
    nodes: tuple[NodePlan, ...] = ()

    @property
    def deleted_ids(self) -> tuple[int, ...]:
        return tuple(n.location_id for n in self.nodes if n.action == "delete")

    @property
    def retired_ids(self) -> tuple[int, ...]:
        return tuple(n.location_id for n in self.nodes if n.action == "retire")


# ---------------------------------------------------------------------------
# What names a location
# ---------------------------------------------------------------------------


def _held_lots(session: Session, location_ids: list[int]) -> dict[int, list[StockLot]]:
    """Every lot sitting at each of `location_ids`, at any balance.

    Both balances matter and mean different things. A **non-zero** lot is stock
    physically in the way and blocks the removal outright. A **zero** lot is an
    emptied package whose row still exists — nothing deletes a lot — so it does
    not block, but it does pin its location against the `RESTRICT`.
    """
    if not location_ids:
        return {}
    held: dict[int, list[StockLot]] = {}
    rows = session.execute(
        select(StockLot).where(StockLot.location_id.in_(location_ids)).order_by(StockLot.id)
    ).scalars()
    for lot in rows:
        held.setdefault(lot.location_id, []).append(lot)
    return held


def _in_ledger(session: Session, location_ids: list[int]) -> set[int]:
    """Which of these locations any ledger row names as a source or destination.

    Checked up front rather than caught as an `IntegrityError`, so the answer is
    "the ledger records movements through this drawer" and not "constraint
    failed". Same call `projects._location_is_referenced` makes, kept separate
    because that one folds the lot check in and this one has to report the two
    apart.
    """
    if not location_ids:
        return set()
    wanted = set(location_ids)
    named: set[int] = set()
    for source, destination in session.execute(
        select(StockLedger.from_location_id, StockLedger.to_location_id).where(
            or_(
                StockLedger.from_location_id.in_(location_ids),
                StockLedger.to_location_id.in_(location_ids),
            )
        )
    ).all():
        # A movement names two locations and only one of them need be in the
        # subtree, so both ends are filtered rather than assumed.
        named |= {end for end in (source, destination) if end in wanted}
    return named


def _tagged(session: Session, location_ids: list[int]) -> set[int]:
    if not location_ids:
        return set()
    return set(
        session.execute(
            select(LocationTag.location_id).where(LocationTag.location_id.in_(location_ids))
        )
        .scalars()
        .all()
    )


def _printed(session: Session, locations: list[Location]) -> set[int]:
    """Which of these have had a label card physically produced.

    Two independent records of the same fact, and either is enough:
    `locations.last_printed_at`, set by the label sheet route, and a
    `label_prints` row, which is per printed object across all history. A
    `short_id` on its own is deliberately *not* in here — see the module
    docstring.
    """
    printed = {loc.id for loc in locations if loc.last_printed_at is not None}
    ids = [loc.id for loc in locations]
    if ids:
        printed |= set(
            session.execute(
                select(LabelPrint.entity_pk).where(
                    LabelPrint.entity_type == EntityType.LOCATION,
                    LabelPrint.entity_pk.in_(ids),
                )
            )
            .scalars()
            .all()
        )
    return printed


def _describe_lots(session: Session, lots: list[StockLot]) -> str:
    """Prose like `470 x C0603C104K (lot 12)` — what is actually in the drawer.

    Reads the cached balance, never a ledger sum: `stock_lots.qty_milli_cached`
    is the only sanctioned source of a balance in a request path.
    """
    names: list[str] = []
    for lot in lots[:_MAX_NAMED]:
        part = session.get(Part, lot.part_id)
        label = (part.mpn or part.name) if part is not None else f"part {lot.part_id}"
        whole = lot.qty_milli_cached / 1000
        amount = f"{whole:g}"
        names.append(f"{amount} x {label} (lot {lot.id})")
    if len(lots) > _MAX_NAMED:
        names.append(f"and {len(lots) - _MAX_NAMED} more lot(s)")
    return ", ".join(names)


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_removal(session: Session, root: Location, *, recursive: bool) -> RemovalPlan:
    """Work out, without writing anything, what removing `root` would do.

    Read-only and safe to call from a preview route, which is the point: the
    confirm dialog and the write path derive the same answer from the same
    function, so the dialog cannot promise something the delete then refuses.

    `recursive=False` and a subtree of more than one node is a refusal that
    names the children, never a silent recursion — deleting a cabinet must be an
    explicit decision about the drawers in it.
    """
    subtree = location_tree(session).subtree(root)
    # `subtree` comes back ordered by `id_path`, i.e. parents before children.
    # Reversed, that is deepest-first: the order deletes have to reach SQL in
    # (`locations.parent_id` is RESTRICT) and the order the "a kept child pins
    # its parent" rule needs to see nodes in.
    deepest_first = list(reversed(subtree))
    ids = [loc.id for loc in deepest_first]

    blockers: list[Blocker] = []
    lots_by_location = _held_lots(session, ids)
    for loc in deepest_first:
        occupied = [lot for lot in lots_by_location.get(loc.id, []) if lot.qty_milli_cached != 0]
        if occupied:
            blockers.append(
                Blocker(
                    reason="holds_stock",
                    location_id=loc.id,
                    label=loc.name,
                    label_path=loc.label_path,
                    detail=_describe_lots(session, occupied),
                )
            )

    if blockers:
        raise RemovalRefused(
            "holds_stock",
            _stock_message(root, blockers),
            tuple(blockers),
        )

    descendants = [loc for loc in deepest_first if loc.id != root.id]
    if descendants and not recursive:
        raise RemovalRefused(
            "has_children",
            _children_message(root, descendants),
            tuple(
                Blocker(
                    reason="has_children",
                    location_id=loc.id,
                    label=loc.name,
                    label_path=loc.label_path,
                    detail="would be removed too",
                )
                for loc in descendants[:_MAX_NAMED]
            ),
        )

    in_ledger = _in_ledger(session, ids)
    tagged = _tagged(session, ids)
    printed = _printed(session, deepest_first)

    nodes: list[NodePlan] = []
    #: A node that keeps its row keeps every ancestor's row too — `parent_id` is
    #: RESTRICT, so deleting the parent of a retired child fails at commit and
    #: rolls the whole request back. `projects._remove_unreferenced_staging_boxes`
    #: learned this the hard way; deepest-first ordering alone does not express it.
    pinned_by_child: set[int] = set()
    for loc in deepest_first:
        pins: list[str] = []
        if lots_by_location.get(loc.id):
            pins.append("has_lots")
        if loc.id in in_ledger:
            pins.append("in_ledger")
        if loc.id in printed:
            pins.append("printed")
        if loc.id in tagged:
            pins.append("bound_tag")
        if loc.id in pinned_by_child:
            pins.append("pinned_by_child")
        action = "retire" if pins else "delete"
        if action == "retire" and loc.parent_id is not None:
            pinned_by_child.add(loc.parent_id)
        nodes.append(
            NodePlan(
                location_id=loc.id,
                label=loc.name,
                label_path=loc.label_path,
                action=action,
                pins=tuple(pins),
            )
        )

    return RemovalPlan(root_id=root.id, nodes=tuple(nodes))


def _stock_message(root: Location, blockers: list[Blocker]) -> str:
    if len(blockers) == 1 and blockers[0].location_id == root.id:
        return f"{root.name} still holds {blockers[0].detail}"
    named = "; ".join(f"{b.label_path} holds {b.detail}" for b in blockers[:_MAX_NAMED])
    more = (
        f"; and {len(blockers) - _MAX_NAMED} more location(s)" if len(blockers) > _MAX_NAMED else ""
    )
    return (
        f"{root.name} cannot be removed because stock is still inside it: {named}{more}."
        " Move it somewhere else first — nothing here relocates stock on its own,"
        " because where it goes is a movement and your decision."
    )


def _children_message(root: Location, descendants: list[Location]) -> str:
    named = ", ".join(loc.name for loc in descendants[:_MAX_NAMED])
    more = f" and {len(descendants) - _MAX_NAMED} more" if len(descendants) > _MAX_NAMED else ""
    return (
        f"{root.name} still has {len(descendants)} container(s) inside it ({named}{more})."
        " Remove them too by confirming a recursive removal, or empty it out first."
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_removal(session: Session, plan: RemovalPlan) -> RemovalPlan:
    """Carry out `plan`. Returns it unchanged, so a caller can render one answer.

    Does not commit — the route owns the transaction, as every other write in
    this package does.
    """
    now = datetime.now(UTC)
    for node in plan.nodes:
        location = session.get(Location, node.location_id)
        if location is None:  # pragma: no cover - planned from live rows
            continue
        if node.action == "retire":
            retire(location, at=now)
        else:
            _delete(session, location)

    # One statement, unconditional, after the whole plan: a delete changes no
    # other row's `parent_id`, but a retire clears a slot label and therefore a
    # name nothing else recomputes, and rebuilding is idempotent either way.
    location_tree(session).rebuild_paths()
    return plan


def retire(location: Location, *, at: datetime | None = None) -> None:
    """Take a container out of the tree while keeping its row and its history.

    Three columns are cleared alongside the timestamp, and each is what makes
    the container actually *leave* rather than merely being flagged:

    * `slot_label` — there is a partial unique index on `(parent_id,
      slot_label)`, so a retired drawer that kept the name `B3` would block ever
      laying out a `B3` there again. Its `name` still says `B3`, which is where
      that fact belongs once the row is no longer a cell of anyone's canvas.
    * `row_idx` / `col_idx` — `_layout_read`, `labels._slot_children` and
      `diff_instance_layout` all already skip a child with no cell, so clearing
      these removes the row from the layout editor, the label sheet and the
      provisioning walk without touching any of them.

    The floor-plan placement goes for the same reason: a retired cabinet must
    not still be drawn standing in the room.
    """
    location.retired_at = at if at is not None else datetime.now(UTC)
    location.slot_label = None
    location.row_idx = None
    location.col_idx = None
    room_plan.forget_placement(location)


def _delete(session: Session, location: Location) -> None:
    """Delete one location row, having established that nothing names it.

    Two tables reference a location by `(entity_type, entity_pk)` with **no
    foreign key**, so nothing at the database layer stops them being orphaned:
    `object_ids` — where an orphan is the bad case, a code that still resolves to
    a row that is gone — and `label_prints`. Both are reaped here explicitly.
    Everything else referencing `locations.id` is either `CASCADE`
    (`location_tags`, `location_occupancy`, provisioning and verification rows,
    label sheet jobs, layout suggestions), `SET NULL` (`projects.
    staging_location_id`, which its own docstring calls the ordinary case of
    removing an empty project box), or `RESTRICT` against something the plan has
    already established is absent.

    **The flush is load-bearing, not belt-and-braces.** `locations` has no
    `relationship()` for `parent_id` — the tree is driven by the path cache, not
    by ORM cascades — so the unit of work has no dependency to sort on and
    batches same-table deletes into one `executemany` in arbitrary order.
    Parent-before-child then trips the `RESTRICT` even when every node is
    removable. Deleting one row at a time is the only way deepest-first ordering
    reaches SQL at all.
    """
    session.execute(
        delete(ObjectId).where(
            ObjectId.entity_type == EntityType.LOCATION, ObjectId.entity_pk == location.id
        )
    )
    session.execute(
        delete(LabelPrint).where(
            LabelPrint.entity_type == EntityType.LOCATION, LabelPrint.entity_pk == location.id
        )
    )
    session.delete(location)
    session.flush()


class RestoreRefused(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def restore(session: Session, location: Location) -> list[Location]:
    """Undo a retirement, for `location` and everything retired under it.

    The whole retired subtree, because retiring a cabinet retired its drawers
    with it and bringing back the cabinet alone would leave them stranded
    invisible inside a visible container.

    What does **not** come back is where it stood: a retirement cleared the slot
    cell and the floor-plan coordinate, and the cell may well be occupied by now.
    A restored container is an unplaced child of the same parent — visible,
    findable, and needing one drag or one layout edit to be somewhere again.
    Silently reclaiming a cell somebody has since laid out is exactly the
    "silently redefine what a label means" failure the layout change guard
    exists to prevent.
    """
    if location.retired_at is None:
        raise RestoreRefused("not_retired", f"{location.name} has not been removed")
    tree = location_tree(session)
    for ancestor in tree.ancestors(location):
        if ancestor.retired_at is not None:
            raise RestoreRefused(
                "ancestor_retired",
                f"{ancestor.name} was removed too, so restore that first"
                f" — {location.name} would have nowhere visible to sit.",
            )
    restored = [loc for loc in tree.subtree(location) if loc.retired_at is not None]
    for loc in restored:
        loc.retired_at = None
    session.flush()
    return restored


__all__ = [
    "Blocker",
    "NodePlan",
    "RemovalPlan",
    "RemovalRefused",
    "RestoreRefused",
    "apply_removal",
    "plan_removal",
    "restore",
    "retire",
]
