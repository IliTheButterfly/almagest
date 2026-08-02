/**
 * The walk, as data.
 *
 * Three properties, and each of them is a way the panel would lie rather than a
 * way it would look wrong:
 *
 * * the turns are root-first and pair each level with the child to head for —
 *   reverse them and the panel walks somebody out of the building;
 * * a destination the tree does not hold produces **no** walk, not a partial one
 *   starting in mid-air;
 * * a cycle terminates, because a `parent_id` loop is impossible by construction
 *   and an infinite loop in a walk is a white screen on a phone.
 */

import { describe, expect, it } from "vitest";

import { directionsSentence, directionsTo, siblingCount } from "./directions";
import { indexTree } from "./tree";
import type { LocationNode } from "../api/client";

function node(id: number, name: string, parentId: number | null): LocationNode {
  return {
    id,
    name,
    parent_id: parentId,
    depth: 0,
    id_path: `/${id}/`,
    label_path: name,
    container_type_id: null,
    slot_label: null,
    is_placeable: null,
    effective_child_view: "list",
    child_grid_rows: null,
    child_grid_cols: null,
    effective_glyph: null,
    is_overfull: false,
    is_staging: false,
    fill_ratio: 0,
    lot_count: 0,
    qty_milli: 0,
    retired_at: null,
  };
}

const WORKSHOP = node(1, "Workshop", null);
const SHED = node(2, "Shed", null);
const CABINET = node(10, "Cabinet A", 1);
const DRAWER = node(20, "Drawer A2", 10);
const INDEX = indexTree([WORKSHOP, SHED, CABINET, DRAWER]);

describe("directionsTo", () => {
  it("walks in from the top level, pairing each level with the child to head for", () => {
    const walk = directionsTo(INDEX, 20);

    expect(walk.map((step) => [step.at?.name ?? null, step.to.name])).toEqual([
      [null, "Workshop"],
      ["Workshop", "Cabinet A"],
      ["Cabinet A", "Drawer A2"],
    ]);
    // Exactly one last turn, and it is the destination — the panel keys its
    // "open this container" link off it.
    expect(walk.filter((step) => step.last).map((step) => step.to.id)).toEqual([20]);
  });

  it("reads as instructions rather than as a path", () => {
    expect(directionsSentence(directionsTo(INDEX, 20))).toBe(
      "Workshop, then Cabinet A, then Drawer A2",
    );
  });

  it("gives one turn for a top-level container", () => {
    expect(directionsTo(INDEX, 1).map((step) => step.to.name)).toEqual(["Workshop"]);
  });

  it("refuses to guess when the tree does not hold the destination", () => {
    // The caller falls back to the plain `label_path` it already had. Half a
    // walk would point confidently at the wrong cabinet.
    expect(directionsTo(INDEX, 999)).toEqual([]);
  });

  it("stops rather than looping when a parent chain cycles", () => {
    const a = { ...node(30, "A", 31), parent_id: 31 };
    const b = { ...node(31, "B", 30), parent_id: 30 };
    const walk = directionsTo(indexTree([a, b]), 30);

    expect(walk.length).toBeLessThanOrEqual(2);
    expect(walk[walk.length - 1]?.to.id).toBe(30);
  });
});

describe("siblingCount", () => {
  it("counts what is at the same level, which is what makes a picture worth drawing", () => {
    const walk = directionsTo(INDEX, 20);

    // Two sheds to choose between at the top; one cabinet inside the workshop,
    // so that level is a sentence rather than a map.
    expect(siblingCount(INDEX, walk[0]!)).toBe(2);
    expect(siblingCount(INDEX, walk[1]!)).toBe(1);
  });
});
