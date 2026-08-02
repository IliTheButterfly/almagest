"""One tree implementation, shared by both hierarchies.

`locations` (physical) and `part_categories` (logical) are the same structure
with different meanings, so this is written once and parameterised by table.

**Adjacency list plus a derived path cache.** Not nested sets, where a subtree
move renumbers the table; not a closure table, which is machinery with no
payoff at ~10³ nodes when SQLite has recursive CTEs.

The cache — `depth`, `id_path`, `label_path` — is 100% reconstructible from
`parent_id` alone. That is the property everything else leans on: a cache bug
is a stale label, never data loss, and the escape hatch is a sub-second full
rebuild.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.orm import Session

from app.models.base import LABEL_SEP, PATH_SEP
from app.models.catalog import PartCategory
from app.models.storage import Location
from app.services import room_plan

#: Table names are our own constants, never user input — but they are
#: interpolated into SQL, so they are checked anyway rather than trusted.
_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CycleError(ValueError):
    """A move that would make a node its own ancestor."""


#: Constrained rather than bound: these are the only two trees, and listing
#: them keeps `nearest_ancestor_value` and friends checkable against the real
#: column sets instead of a structural guess.
class TreeRepository[TreeNode: (Location, PartCategory)]:
    def __init__(self, session: Session, model: type[TreeNode]) -> None:
        table = model.__tablename__
        if not _SAFE_TABLE.match(table):
            raise ValueError(f"unsafe table name {table!r}")
        self.session = session
        self.model: type[TreeNode] = model
        self.table = table

    # -- reads ------------------------------------------------------------

    def children(self, node_id: int | None) -> list[TreeNode]:
        stmt: Select[tuple[TreeNode]] = select(self.model).where(self.model.parent_id == node_id)
        return list(self.session.execute(stmt.order_by(self.model.id)).scalars())

    def subtree_all(self) -> list[TreeNode]:
        """Every node in the table, ordered by id. For rebuilds and audits."""
        stmt: Select[tuple[TreeNode]] = select(self.model).order_by(self.model.id)
        return list(self.session.execute(stmt).scalars())

    def subtree(self, node: TreeNode, *, include_self: bool = True) -> list[TreeNode]:
        """Everything at or below `node`.

        `id_path LIKE :prefix || '%'` — left-anchored, so the index serves it.
        The separators wrapping every id are what stop `/1/` matching `/12/`.
        """
        prefix = node.id_path
        stmt: Select[tuple[TreeNode]] = select(self.model).where(
            self.model.id_path.like(f"{prefix}%")
        )
        rows = list(self.session.execute(stmt.order_by(self.model.id_path)).scalars())
        if not include_self:
            rows = [row for row in rows if row.id != node.id]
        return rows

    def ancestors(self, node: TreeNode) -> list[TreeNode]:
        """Root-first, excluding `node` itself.

        Derived from the ids already encoded in `id_path`, so this is one
        indexed lookup per level and needs no recursion at all.
        """
        ids = self.path_ids(node)[:-1]
        if not ids:
            return []
        stmt: Select[tuple[TreeNode]] = select(self.model).where(self.model.id.in_(ids))
        by_id = {row.id: row for row in self.session.execute(stmt).scalars()}
        return [by_id[i] for i in ids if i in by_id]

    @staticmethod
    def path_ids(node: TreeNode) -> list[int]:
        """The ids in `id_path`, root first, ending with the node's own."""
        return [int(part) for part in node.id_path.split(PATH_SEP) if part]

    def nearest_ancestor_value(self, node: TreeNode, attribute: str) -> Any:
        """Walk up until an attribute is not NULL, and return it.

        This is how inherited properties work — most importantly `esd_safe`,
        where marking a whole cabinet ESD-safe has to be one edit rather than
        one per drawer.
        """
        own = getattr(node, attribute)
        if own is not None:
            return own
        for ancestor in reversed(self.ancestors(node)):
            value = getattr(ancestor, attribute)
            if value is not None:
                return value
        return None

    # -- writes -----------------------------------------------------------

    def would_create_cycle(self, node_id: int, new_parent_id: int | None) -> bool:
        """Walk `parent_id` upward from the proposed parent.

        **Deliberately not the `id_path LIKE` test the design sketches.** That
        test consults the cache, and the cache is exactly the thing that can be
        stale — while a cycle admitted through a stale guard makes the rebuild
        CTE recurse forever, which is unrecoverable without manual surgery. The
        adjacency list is authoritative, and at ~10 levels deep walking it costs
        nothing.

        The `visited` set means a cycle that somehow already exists is detected
        rather than hung on.
        """
        if new_parent_id is None:
            return False
        if new_parent_id == node_id:
            return True

        visited: set[int] = set()
        cursor: int | None = new_parent_id
        while cursor is not None:
            if cursor == node_id:
                return True
            if cursor in visited:
                return True
            visited.add(cursor)
            cursor = self.session.execute(
                text(f"SELECT parent_id FROM {self.table} WHERE id = :id"),
                {"id": cursor},
            ).scalar_one_or_none()
        return False

    def move(self, node: TreeNode, new_parent_id: int | None) -> None:
        """Reparent, then refresh the cache for everything affected."""
        old_parent_id = node.parent_id
        if self.would_create_cycle(node.id, new_parent_id):
            raise CycleError(
                f"moving {self.table} {node.id} under {new_parent_id} would create a cycle"
            )
        if isinstance(node, Location) and node.plan_parent_id != new_parent_id:
            # A floor-plan coordinate belongs to one parent and is meaningless in
            # another (ADR 0009), so a reparent drops it. This lives in the
            # generic repository rather than in `room_plan` because this method is
            # the *only* reparent path in the codebase, and the alternative is a
            # rule every future caller has to remember. It is not what makes the
            # invalidation correct — `room_plan.placement_of()` already ignores a
            # placement whose `plan_parent_id` no longer matches, so a reparent
            # written any other way is still safe. This just stops the row
            # carrying dead coordinates.
            room_plan.forget_placement(node)
        node.parent_id = new_parent_id
        self.session.flush()
        self.rebuild_paths()
        # Both ends: the cabinet it left has one fewer child, the one it joined
        # has one more, and each has its own ancestors.
        self._mark_occupancy_dirty(node)
        if old_parent_id is not None and isinstance(node, Location):
            from app.db.maintenance import mark_location_occupancy_dirty

            mark_location_occupancy_dirty(self.session, [old_parent_id])

    def rebuild_paths(self) -> int:
        """Recompute `depth`, `id_path` and `label_path` for the whole table.

        One recursive CTE and one `UPDATE ... FROM`. Rebuilding everything
        rather than just the moved subtree is a deliberate simplification: at
        ~10³ nodes it is a single sub-second statement, and a partial update is
        where the subtle bugs live. Correctness first; the whole-table rebuild
        *is* the optimisation, compared to recursing per row.

        Idempotent by construction — the output depends only on `parent_id` and
        `name`, so running it twice changes nothing the first run did not.

        Returns the number of rows that were **stale**, which doubles as a
        drift measure for the nightly cache check. It is counted before the
        update rather than read from `rowcount`, because SQLite reports no
        row count for a `WITH ... UPDATE` issued as a text statement.
        """
        stale = int(self.session.execute(text(self._sql("count"))).scalar_one())
        if stale:
            self.session.execute(text(self._sql("update")))
            self.session.expire_all()
        return stale

    def _sql(self, mode: str) -> str:
        # The table name is validated against `_SAFE_TABLE` in __init__; the
        # separators are module constants. Nothing here is caller-supplied.
        cte = f"""
            WITH RECURSIVE tree(id, depth, id_path, label_path) AS (
                SELECT id,
                       0,
                       '{PATH_SEP}' || id || '{PATH_SEP}',
                       name
                  FROM {self.table}
                 WHERE parent_id IS NULL
                UNION ALL
                SELECT child.id,
                       parent.depth + 1,
                       parent.id_path || child.id || '{PATH_SEP}',
                       parent.label_path || '{LABEL_SEP}' || child.name
                  FROM {self.table} AS child
                  JOIN tree AS parent ON child.parent_id = parent.id
            )
        """
        # `IS NOT` rather than `<>` so a NULL on either side compares correctly
        # instead of making the whole predicate NULL.
        differs = f"""
            {self.table}.depth IS NOT tree.depth
         OR {self.table}.id_path IS NOT tree.id_path
         OR {self.table}.label_path IS NOT tree.label_path
        """
        if mode == "count":
            return f"""{cte}
                SELECT COUNT(*)
                  FROM tree
                  JOIN {self.table} ON tree.id = {self.table}.id
                 WHERE {differs}
            """
        return f"""{cte}
            UPDATE {self.table}
               SET depth = tree.depth,
                   id_path = tree.id_path,
                   label_path = tree.label_path
              FROM tree
             WHERE tree.id = {self.table}.id
               AND ({differs})
        """

    def insert_and_index(self, node: TreeNode) -> TreeNode:
        """Add a node and give it a correct path immediately."""
        self.session.add(node)
        self.session.flush()
        self.rebuild_paths()
        self._mark_occupancy_dirty(node)
        return node

    def _mark_occupancy_dirty(self, node: TreeNode) -> None:
        """A container's fill now depends on how many children it has.

        The `location_occupancy` triggers cover the ledger and lot relocation —
        "the one case a trigger cannot reach" in
        `maintenance.mark_location_occupancy_dirty`'s words — and adding,
        moving or retiring a *container* is exactly that case. It became
        load-bearing when the slots model started counting child containers: a
        cabinet's stored fill was written once, at creation, and then never
        again, so the storage map read 0% for a full cabinet for ever while its
        own page read 100%. Only the bulk pass writes `is_overfull`, so an
        over-filled cabinet could not be flagged either.

        `PartCategory` shares this repository and has no occupancy, hence the
        isinstance — the same guard `move` already uses for floor-plan
        coordinates.
        """
        if not isinstance(node, Location):
            return
        from app.db.maintenance import mark_location_occupancy_dirty

        # The node itself and its parent: the child's own fill is unchanged by
        # being inserted, but its parent's is, and the helper walks ancestors.
        mark_location_occupancy_dirty(
            self.session, [i for i in (node.id, node.parent_id) if i is not None]
        )


def location_tree(session: Session) -> TreeRepository[Location]:
    return TreeRepository(session, Location)


def category_tree(session: Session) -> TreeRepository[PartCategory]:
    return TreeRepository(session, PartCategory)
