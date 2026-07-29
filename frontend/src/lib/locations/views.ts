/**
 * Which picture a level of the storage tree is drawn as — ADR 0006.
 *
 * The backend has already resolved it. `LocationNode.effective_child_view` is
 * the instance override, else the container type's, else derived from the type's
 * declared geometry (`app/services/views.py`), and it is resolved there rather
 * than here for two reasons: the fallback reads `container_types`, which a tree
 * render would otherwise have to fetch and join for itself, and a second copy of
 * a three-rung rule is a second copy to disagree with.
 *
 * So the only thing this module does is answer the question **for the container
 * whose children are being drawn**, given the tree index and its id. That is
 * deliberately one expression, because it is the enforcement point for the
 * recursion claim: every level, at every depth, is drawn by calling this with
 * that level's own id. A renderer that asked "am I at the top?" or "is this a
 * cabinet?" would grow a special case per level and contradict a schema that has
 * no named levels at all.
 */

import type { ChildView } from "../api/client";
import type { SlotCanvas } from "./slots";
import type { TreeIndex } from "./tree";

/** Re-exported so a component needs one import, not two. Generated, never hand-written. */
export type { ChildView };

export const FLOOR_PLAN: ChildView = "floor_plan";

/**
 * Whether this view places children at authored row/column positions.
 *
 * The one behavioural consequence of the axis: only these two draw a grid, and
 * only these two draw the positions that are *empty* — "drawer B3 has nothing in
 * it" is a fact about the furniture, whereas a room has no position to be empty,
 * so inventing one there would be inventing furniture.
 */
export function isSlotted(view: ChildView): boolean {
  return view === "cabinet_face" || view === "grid_cells";
}

/** What to call it on screen. */
export const VIEW_LABELS: Readonly<Record<ChildView, string>> = {
  floor_plan: "Floor plan — things standing in a space",
  shelf_run: "Shelves — one run per level",
  cabinet_face: "Cabinet face — drawer fronts",
  grid_cells: "Grid — cells seen from above",
  list: "List — rows, no geometry",
};

/**
 * The sentence under a level, saying what it is drawn as and why it looks that
 * way. Every view says something: a room drawn as a plan is not a grid that
 * failed, and framing it as one would cry wolf.
 */
export const VIEW_NOTES: Readonly<Record<ChildView, string>> = {
  floor_plan:
    "Drawn as a floor plan: these are placed rather than slotted, so there are no rows " +
    "and columns — and no empty positions, because a space has none. Listed in the order " +
    "they were added.",
  shelf_run: "Drawn as shelves, one run per level. How many stand on a level is not fixed.",
  cabinet_face: "Drawn as a cabinet face — drawer fronts, including the drawers that are empty.",
  grid_cells: "Drawn as a grid of cells seen from above, including the cells that are empty.",
  list: "Drawn as rows: what is in here has an order and no geometry worth drawing.",
};

/**
 * Unknown view kinds are drawn as a list rather than not at all.
 *
 * `container_types.child_view` and `locations.child_view` carry no `CHECK`, which
 * is what makes adding a drawing kind a one-line change — and the price of that
 * is that a row written by a newer build can name a kind this bundle has never
 * heard of. The server passes such a value through untouched on purpose, so the
 * client owes it a picture.
 */
export function known(view: string): ChildView {
  return view in VIEW_LABELS ? (view as ChildView) : "list";
}

/**
 * How `parentId`'s children are drawn.
 *
 * `parentId === null` is **the world**: the outermost level, whose children are
 * the roots of the tree. It resolves to a floor plan through the same rule as any
 * container that presents no grid and occupies none — the world holds furniture —
 * which is what `resolve_child_view(None, None)` returns on the server, and the
 * reason depth 0 needs no special case. An id that is not in the index (a subtree
 * fetch whose root's parent is outside the result set) is the same situation.
 */
export function childViewOf(index: TreeIndex, parentId: number | null): ChildView {
  if (parentId === null) {
    return FLOOR_PLAN;
  }
  const node = index.byId.get(parentId);
  return node === undefined ? FLOOR_PLAN : known(node.effective_child_view);
}

/**
 * The canvas `parentId` declares for its children, or `null` if it declares none.
 *
 * The companion to `childViewOf`, and here rather than in `slots.ts` for the same
 * reason that one is here: it is a question asked *of a level* about how its own
 * children are drawn, and the server resolves both off the same container type
 * row. `derive_child_view` reads `grid_rows`/`grid_cols` to answer `cabinet_face`,
 * so the client needs them to honour that answer for a level whose slot labels
 * are a plain sequence — see `SlotCanvas`.
 *
 * `null` for the world, for a node outside the index, and for a level that
 * declares no canvas. All three mean the same thing to the renderer: no authored
 * geometry, so none is invented.
 */
export function childCanvasOf(index: TreeIndex, parentId: number | null): SlotCanvas | null {
  if (parentId === null) {
    return null;
  }
  const node = index.byId.get(parentId);
  if (node === undefined) {
    return null;
  }
  const { child_grid_rows: rows, child_grid_cols: cols } = node;
  if (rows === null || cols === null || rows < 1 || cols < 1) {
    return null;
  }
  return { rows, cols };
}
