/**
 * The slot-label reading, which is the load-bearing guess in the storage map.
 *
 * Two failure modes matter, and they are not symmetric. Failing to build a grid
 * costs a nicer view. Building the *wrong* grid puts a drawer in the wrong place
 * on screen, which is worse than a list — so every ambiguous case here has to
 * come out as an admitted fallback rather than a confident answer.
 */

import { describe, expect, it } from "vitest";

import { inferLayout, parseSequenceLabel, parseSlotLabel, slotLabelFor } from "./slots";

interface Bin {
  readonly id: number;
  readonly slot_label: string | null;
}

const bin = (id: number, slot_label: string | null): Bin => ({ id, slot_label });
const layoutOf = (bins: readonly Bin[]) => inferLayout(bins, (child) => child.slot_label);

describe("parseSlotLabel", () => {
  it("reads the scheme the backend generates", () => {
    // services/assignment._next_grid_slot: chr(ord('A') + row) + str(col + 1).
    expect(parseSlotLabel("A1")).toEqual({ row: 0, col: 0 });
    expect(parseSlotLabel("A2")).toEqual({ row: 0, col: 1 });
    expect(parseSlotLabel("B1")).toEqual({ row: 1, col: 0 });
    expect(parseSlotLabel("C12")).toEqual({ row: 2, col: 11 });
  });

  it("tolerates what a human writes on a label maker", () => {
    for (const label of ["a1", " A1 ", "A-1", "A_1", "A 1", "A.1"]) {
      expect(parseSlotLabel(label), label).toEqual({ row: 0, col: 0 });
    }
  });

  it("carries on past twenty-six rows, spreadsheet style", () => {
    expect(parseSlotLabel("Z1")).toEqual({ row: 25, col: 0 });
    expect(parseSlotLabel("AA1")).toEqual({ row: 26, col: 0 });
    expect(parseSlotLabel("AD7")).toEqual({ row: 29, col: 6 });
  });

  it("refuses anything that is not this scheme", () => {
    for (const label of ["", "1", "12", "shelf", "A", "A0", "1A", "R1C3", "A1B", null, undefined]) {
      expect(parseSlotLabel(label), String(label)).toBeNull();
    }
  });

  it("round-trips through slotLabelFor", () => {
    for (const label of ["A1", "B3", "Z9", "AA1", "AD7"]) {
      const position = parseSlotLabel(label);
      expect(position).not.toBeNull();
      expect(slotLabelFor(position!)).toBe(label);
    }
  });
});

describe("parseSequenceLabel", () => {
  it("reads a plain number as a zero-based index", () => {
    expect(parseSequenceLabel("1")).toBe(0);
    expect(parseSequenceLabel("12")).toBe(11);
  });

  it("refuses zero and non-numbers", () => {
    for (const label of ["0", "A1", "", "1.5", null]) {
      expect(parseSequenceLabel(label), String(label)).toBeNull();
    }
  });
});

describe("inferLayout on a labelled grid", () => {
  it("places children at their real row and column", () => {
    const bins = [bin(1, "A1"), bin(2, "A2"), bin(3, "B1"), bin(4, "B2")];
    const layout = layoutOf(bins);

    expect(layout.kind).toBe("grid");
    expect([layout.rows, layout.cols]).toEqual([2, 2]);
    expect(layout.cells.map((cell) => cell.node?.id)).toEqual([1, 2, 3, 4]);
    expect(layout.cells[2]).toMatchObject({ row: 1, col: 0 });
  });

  it("sizes the grid from the largest label, not from the child count", () => {
    // A cabinet with two drawers fitted, in row A and row C: three rows exist
    // even though only two are occupied, because that is the furniture.
    const layout = layoutOf([bin(1, "A1"), bin(2, "C1")]);

    expect(layout.kind).toBe("grid");
    expect([layout.rows, layout.cols]).toEqual([3, 1]);
    expect(layout.cells).toHaveLength(3);
  });

  it("materialises the gaps as empty cells with the label they would have", () => {
    const layout = layoutOf([bin(1, "A1"), bin(2, "B2")]);

    expect(layout.cells.map((cell) => [cell.slotLabel, cell.node?.id ?? null])).toEqual([
      ["A1", 1],
      ["A2", null],
      ["B1", null],
      ["B2", 2],
    ]);
    // An empty position is a real place with nothing in it, and the UI has to be
    // able to say the label is its own guess.
    expect(layout.cells[1]?.inferredLabel).toBe(true);
    expect(layout.cells[0]?.inferredLabel).toBe(false);
  });

  it("prints the label that is physically on the drawer, not a normalised one", () => {
    const layout = layoutOf([bin(1, "a-1"), bin(2, "B2")]);
    expect(layout.cells[0]?.slotLabel).toBe("a-1");
  });

  it("keeps unparseable children, separately, rather than dropping them", () => {
    const layout = layoutOf([bin(1, "A1"), bin(2, "A2"), bin(3, "loose parts")]);

    expect(layout.kind).toBe("grid");
    expect(layout.cells.map((cell) => cell.node?.id)).toEqual([1, 2]);
    expect(layout.unplaced.map((child) => child.id)).toEqual([3]);
  });
});

describe("inferLayout falling back", () => {
  it("flows a plain-numbered container in numeric order", () => {
    // "10" sorts before "9" as text; the point of parsing it is that it doesn't.
    const layout = layoutOf([bin(1, "9"), bin(2, "10"), bin(3, "1")]);

    expect(layout.kind).toBe("sequence");
    expect(layout.reason).toBeNull();
    expect(layout.cells.map((cell) => cell.node?.id)).toEqual([3, 1, 2]);
  });

  it("flows unlabelled children in the order the API returned them", () => {
    const layout = layoutOf([bin(1, null), bin(2, null), bin(3, null)]);

    expect(layout.kind).toBe("flow");
    expect(layout.reason).toBe("unlabelled");
    expect(layout.cells.map((cell) => cell.node?.id)).toEqual([1, 2, 3]);
    expect([layout.rows, layout.cols]).toEqual([0, 0]);
  });

  it("distinguishes 'no labels' from 'labels I cannot read'", () => {
    // The UI marks one as a fallback and leaves the other looking like an
    // ordinary list, so the two must not collapse into one reason.
    expect(layoutOf([bin(1, null)]).reason).toBe("unlabelled");
    expect(layoutOf([bin(1, "top"), bin(2, "bottom")]).reason).toBe("unparsed");
  });

  it("refuses to build a grid when two labels read as the same cell", () => {
    const layout = layoutOf([bin(1, "A1"), bin(2, "a-1"), bin(3, "B1")]);

    expect(layout.kind).toBe("flow");
    expect(layout.reason).toBe("collision");
    // Nothing is lost: every child is still rendered.
    expect(layout.cells.map((cell) => cell.node?.id)).toEqual([1, 2, 3]);
  });

  it("refuses a grid that is implausibly sparse", () => {
    const layout = layoutOf([bin(1, "A1"), bin(2, "Z9")]);

    expect(layout.kind).toBe("flow");
    expect(layout.reason).toBe("implausible");
  });

  it("accepts a large grid when the children actually fill it", () => {
    const bins = Array.from({ length: 96 }, (_, index) =>
      bin(index, `${String.fromCharCode(65 + Math.floor(index / 12))}${(index % 12) + 1}`),
    );
    const layout = layoutOf(bins);

    expect(layout.kind).toBe("grid");
    expect([layout.rows, layout.cols]).toEqual([8, 12]);
    expect(layout.cells.filter((cell) => cell.node !== null)).toHaveLength(96);
  });

  it("handles an empty container without inventing a cell", () => {
    const layout = layoutOf([]);
    expect(layout.cells).toEqual([]);
    expect(layout.unplaced).toEqual([]);
  });
});
