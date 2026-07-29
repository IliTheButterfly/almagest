/**
 * The one expression the recursion rests on.
 *
 * `childViewOf` is called with the id of whichever container is being drawn, at
 * every depth, including the roots — so the properties worth pinning are that it
 * has no depth-dependent behaviour, and that it never returns something the
 * renderer has no branch for.
 */

import { describe, expect, it } from "vitest";

import type { LocationNode } from "../api/client";
import { childViewOf, isSlotted, known, VIEW_LABELS, VIEW_NOTES } from "./views";
import { indexTree } from "./tree";

function node(id: number, parentId: number | null, view: string): LocationNode {
  return {
    id,
    name: `n${id}`,
    parent_id: parentId,
    depth: 0,
    id_path: `/${id}/`,
    label_path: `n${id}`,
    container_type_id: null,
    slot_label: null,
    is_placeable: null,
    effective_child_view: view,
    child_grid_rows: null,
    child_grid_cols: null,
    effective_glyph: null,
    is_overfull: false,
    is_staging: false,
    fill_ratio: null,
    lot_count: 0,
    qty_milli: 0,
  };
}

const TREE = indexTree([
  node(1, null, "floor_plan"),
  node(2, 1, "cabinet_face"),
  node(3, 2, "grid_cells"),
]);

describe("childViewOf", () => {
  it("returns the container's own answer, whatever depth it is at", () => {
    expect(childViewOf(TREE, 1)).toBe("floor_plan");
    expect(childViewOf(TREE, 2)).toBe("cabinet_face");
    expect(childViewOf(TREE, 3)).toBe("grid_cells");
  });

  it("treats the world as a container that presents no grid", () => {
    // Not a special case for depth 0 — the same rule, applied to a level with no
    // container to ask. Matches `resolve_child_view(None, None)` on the server.
    expect(childViewOf(TREE, null)).toBe("floor_plan");
  });

  it("does the same for a parent outside the fetched subtree", () => {
    // `GET /api/locations/tree?root_id=` returns a root whose parent is not in the
    // result set. `indexTree` already treats that parent as absent, so this has to
    // agree with it rather than throw.
    expect(childViewOf(TREE, 999)).toBe("floor_plan");
  });
});

describe("unknown kinds", () => {
  it("fall back to rows rather than to nothing", () => {
    // `child_view` carries no CHECK, so a newer build can name a kind this bundle
    // has never heard of, and the server passes it through untouched on purpose.
    expect(known("isometric_exploded")).toBe("list");
    expect(childViewOf(indexTree([node(1, null, "hologram")]), 1)).toBe("list");
  });

  it("do not swallow a kind that is real", () => {
    for (const kind of Object.keys(VIEW_LABELS)) {
      expect(known(kind)).toBe(kind);
    }
  });
});

describe("isSlotted", () => {
  it("is true for exactly the two views that draw positions", () => {
    // The one behavioural consequence of the axis: only these draw a grid, and
    // only these draw the positions that are empty. A room has no position to be
    // empty, so inventing one there would be inventing furniture.
    const slotted = Object.keys(VIEW_LABELS).filter((kind) =>
      isSlotted(kind as keyof typeof VIEW_LABELS),
    );
    expect(slotted.sort()).toEqual(["cabinet_face", "grid_cells"]);
  });
});

describe("every kind is describable", () => {
  it("has a label and a note, so no view rests on its shape alone", () => {
    for (const kind of Object.keys(VIEW_LABELS)) {
      expect(VIEW_LABELS[kind as keyof typeof VIEW_LABELS]).toBeTruthy();
      expect(VIEW_NOTES[kind as keyof typeof VIEW_NOTES]).toBeTruthy();
    }
    expect(Object.keys(VIEW_NOTES).sort()).toEqual(Object.keys(VIEW_LABELS).sort());
  });
});
