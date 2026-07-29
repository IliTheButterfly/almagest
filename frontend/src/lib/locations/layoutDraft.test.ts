import { describe, expect, it } from "vitest";

import {
  addSlot,
  classifySelection,
  gridExtent,
  mergeSlots,
  previewChanges,
  rectFromCells,
  removeSlot,
  requireLabels,
  splitSlot,
  tilesExactly,
  toSlotSpecIn,
  updateSlot,
  type DraftSlot,
  type OriginalSlot,
} from "./layoutDraft";

function slot(
  id: string,
  rowIdx: number,
  colIdx: number,
  rowSpan = 1,
  colSpan = 1,
  slotLabel = `${id}`,
): DraftSlot {
  return { id, rowIdx, colIdx, rowSpan, colSpan, slotLabel, sizeClass: null, innerVolumeMm3: null };
}

function original(base: DraftSlot, extra: Partial<OriginalSlot> = {}): OriginalSlot {
  return { ...base, locationId: 1, shortId: null, hasTag: false, lotCount: 0, qtyMilli: 0, ...extra };
}

// -------------------------------------------------------------- selection ---

describe("classifySelection", () => {
  const grid = [slot("a1", 0, 0), slot("a2", 0, 1), slot("b1", 1, 0), slot("b2", 1, 1)];

  it("reads a single untouched cell as empty", () => {
    const rect = rectFromCells({ rowIdx: 3, colIdx: 3, rowSpan: 1, colSpan: 1 }, { rowIdx: 3, colIdx: 3, rowSpan: 1, colSpan: 1 });
    expect(classifySelection(grid, rect).kind).toBe("empty");
  });

  it("reads exactly one slot as single, even a 1x1", () => {
    const rect = rectFromCells(grid[0]!, grid[0]!);
    const selection = classifySelection(grid, rect);
    expect(selection.kind).toBe("single");
    if (selection.kind === "single") {
      expect(selection.slot.id).toBe("a1");
    }
  });

  it("reads a 2x2 that tiles exactly as mergeable", () => {
    const rect = rectFromCells(grid[0]!, grid[3]!);
    const selection = classifySelection(grid, rect);
    expect(selection.kind).toBe("mergeable");
    if (selection.kind === "mergeable") {
      expect(selection.slots.map((s) => s.id).sort()).toEqual(["a1", "a2", "b1", "b2"]);
    }
  });

  it("reads a selection that cuts a slot in half as partial, not mergeable", () => {
    // A slot already spanning both rows in column 2, selecting only its top half.
    const spanning = [slot("a1", 0, 0), slot("tall", 0, 1, 2, 1)];
    const rect = rectFromCells({ rowIdx: 0, colIdx: 0, rowSpan: 1, colSpan: 1 }, { rowIdx: 0, colIdx: 1, rowSpan: 1, colSpan: 1 });
    expect(classifySelection(spanning, rect).kind).toBe("partial");
  });

  it("reads a selection mixing a slot with empty space as partial", () => {
    const rect = rectFromCells(grid[0]!, { rowIdx: 0, colIdx: 2, rowSpan: 1, colSpan: 1 });
    expect(classifySelection(grid, rect).kind).toBe("partial");
  });
});

describe("tilesExactly", () => {
  it("is false for an empty list — nothing does not tile anything", () => {
    expect(tilesExactly([], { r0: 0, c0: 0, r1: 0, c1: 0 })).toBe(false);
  });
});

// -------------------------------------------------------------- mutations ---

describe("mergeSlots", () => {
  it("replaces the merged slots with one region spanning their bounding box", () => {
    const grid = [slot("a1", 0, 0), slot("a2", 0, 1), slot("b1", 1, 0), slot("b2", 1, 1)];
    const rect = rectFromCells(grid[0]!, grid[3]!);
    const next = mergeSlots(grid, grid, rect, "A1");

    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ rowIdx: 0, colIdx: 0, rowSpan: 2, colSpan: 2, slotLabel: "A1" });
  });

  it("carries the first absorbed slot's size class and volume forward", () => {
    const a: DraftSlot = { ...slot("a", 0, 0), sizeClass: "small", innerVolumeMm3: 12 };
    const b = slot("b", 0, 1);
    const rect = rectFromCells(a, b);
    const next = mergeSlots([a, b], [a, b], rect, "merged");
    expect(next[0]).toMatchObject({ sizeClass: "small", innerVolumeMm3: 12 });
  });
});

describe("splitSlot", () => {
  it("decomposes a 1x2 merge back into two 1x1 cells at the generated labels", () => {
    const merged = [slot("m", 0, 0, 1, 2, "A1")];
    const next = splitSlot(merged, "m", (r, c) => `${r}-${c}`);

    expect(next).toHaveLength(2);
    expect(next.map((s) => [s.rowIdx, s.colIdx, s.rowSpan, s.colSpan, s.slotLabel])).toEqual([
      [0, 0, 1, 1, "0-0"],
      [0, 1, 1, 1, "0-1"],
    ]);
  });

  it("is a no-op on a slot that is not actually merged", () => {
    const flat = [slot("a", 0, 0)];
    expect(splitSlot(flat, "a", (r, c) => `${r}${c}`)).toEqual(flat);
  });

  it("is a no-op on an unknown id", () => {
    const flat = [slot("a", 0, 0)];
    expect(splitSlot(flat, "missing", (r, c) => `${r}${c}`)).toEqual(flat);
  });
});

describe("removeSlot / addSlot / updateSlot", () => {
  it("removeSlot drops exactly the named slot", () => {
    const grid = [slot("a", 0, 0), slot("b", 0, 1)];
    expect(removeSlot(grid, "a").map((s) => s.id)).toEqual(["b"]);
  });

  it("addSlot appends a new region at the selection", () => {
    const rect = rectFromCells({ rowIdx: 2, colIdx: 2, rowSpan: 1, colSpan: 1 }, { rowIdx: 2, colIdx: 2, rowSpan: 1, colSpan: 1 });
    const next = addSlot([], rect, "C3");
    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ rowIdx: 2, colIdx: 2, slotLabel: "C3" });
  });

  it("updateSlot patches only the named slot", () => {
    const grid = [slot("a", 0, 0), slot("b", 0, 1)];
    const next = updateSlot(grid, "a", { slotLabel: "renamed" });
    expect(next.find((s) => s.id === "a")?.slotLabel).toBe("renamed");
    expect(next.find((s) => s.id === "b")?.slotLabel).toBe("b");
  });
});

describe("gridExtent", () => {
  it("is the bounding box of every slot's full extent, not just its origin", () => {
    expect(gridExtent([slot("a", 0, 0, 2, 3)])).toEqual({ rows: 2, cols: 3 });
  });

  it("is zero by zero for no slots", () => {
    expect(gridExtent([])).toEqual({ rows: 0, cols: 0 });
  });
});

describe("toSlotSpecIn / requireLabels", () => {
  it("sends an empty label as undefined, so a type's canvas lets the server generate it", () => {
    const spec = toSlotSpecIn(slot("a", 0, 0, 1, 1, ""));
    expect(spec.slot_label).toBeUndefined();
  });

  it("flags every blank-labelled slot for the instance path, which has no generator", () => {
    const draft = [slot("a", 0, 0, 1, 1, "A1"), slot("b", 0, 1, 1, 1, "")];
    expect(requireLabels(draft).map((s) => s.id)).toEqual(["b"]);
  });
});

// ------------------------------------------------------------- the preview --

describe("previewChanges", () => {
  it("calls a same-region relabel a safe update, not a delete-plus-create", () => {
    const before = [original(slot("a", 0, 0, 1, 1, "A1"))];
    const after = [slot("a", 0, 0, 1, 1, "shelf-1")];
    const preview = previewChanges(before, after);

    expect(preview.creates).toHaveLength(0);
    expect(preview.deletes).toHaveLength(0);
    expect(preview.updates).toHaveLength(1);
    expect(preview.updates[0]?.after.slotLabel).toBe("shelf-1");
  });

  it("calls a region with no draft counterpart a delete", () => {
    const before = [original(slot("a", 0, 0, 1, 1, "A1"))];
    const preview = previewChanges(before, []);
    expect(preview.deletes.map((s) => s.id)).toEqual(["a"]);
  });

  it("calls a region nothing previously covered a create", () => {
    const preview = previewChanges([], [slot("a", 0, 0, 1, 1, "A1")]);
    expect(preview.creates.map((s) => s.id)).toEqual(["a"]);
  });

  /**
   * The load-bearing case: reusing an existing slot's label at a *different*
   * region must be caught even though the region side of the request also
   * looks exactly like an ordinary delete-plus-create of two unrelated
   * slots. `diff_instance_layout` on the backend refuses this outright
   * rather than silently walking A1's identity somewhere else.
   */
  it("flags a label reused at a different region as reinterpreted, not as delete+create", () => {
    const before = [original(slot("a", 0, 0, 1, 1, "A1")), original(slot("b", 0, 1, 1, 1, "B1"))];
    // "A1" now names what used to be "B1"'s cell.
    const after = [slot("a", 0, 0, 1, 1, "A1"), slot("b", 0, 1, 1, 1, "A1")];
    const preview = previewChanges(before, after);

    expect(preview.reinterpreted).toHaveLength(1);
    expect(preview.reinterpreted[0]?.before.id).toBe("a");
    // "b"'s own region ("B1") is not claimed by anything in the draft — the
    // spec that now sits there claims "A1" instead, which won —, so "b" is
    // *also* gone. This is exactly `diff_instance_layout`'s behaviour: a
    // caller must check `reinterpreted` first and treat the whole request
    // as refused rather than reading this as "one safe delete".
    expect(preview.deletes.map((s) => s.id)).toEqual(["b"]);
  });

  it("carries stock and tag state through on a delete, for the guard preview", () => {
    const before = [original(slot("a", 0, 0, 1, 1, "A1"), { lotCount: 2, qtyMilli: 5000, hasTag: true })];
    const preview = previewChanges(before, []);
    expect(preview.deletes[0]).toMatchObject({ lotCount: 2, qtyMilli: 5000, hasTag: true });
  });

  it("says nothing changed when the draft exactly matches the original", () => {
    const before = [original(slot("a", 0, 0, 1, 1, "A1"))];
    const preview = previewChanges(before, [slot("a", 0, 0, 1, 1, "A1")]);
    expect(preview.creates).toHaveLength(0);
    expect(preview.updates).toHaveLength(0);
    expect(preview.deletes).toHaveLength(0);
    expect(preview.reinterpreted).toHaveLength(0);
  });
});
