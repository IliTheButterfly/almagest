/**
 * The rules that decide which row gets pressed.
 *
 * Ordering is the point, not just membership: the picker shows the first forty
 * matches and the top one is the one a thumb lands on, so "07 outranks the cabinet
 * whose path contains 07" is a behaviour worth pinning, not an implementation
 * detail.
 */

import { describe, expect, it } from "vitest";

import type { LocationNode } from "../api/client";
import { matchLocations, queryTokens } from "./match";

function node(
  id: number,
  name: string,
  labelPath: string,
  extra: Partial<LocationNode> = {},
): LocationNode {
  return {
    id,
    parent_id: null,
    name,
    slot_label: null,
    label_path: labelPath,
    id_path: `/${id}/`,
    depth: labelPath.split(" / ").length - 1,
    lot_count: 0,
    qty_milli: 0,
    fill_ratio: null,
    is_overfull: false,
    is_staging: false,
    is_placeable: true,
    container_type_id: null,
    child_grid_rows: null,
    child_grid_cols: null,
    effective_child_view: "list",
    effective_glyph: null,
    ...extra,
  } as LocationNode;
}

const NODES: readonly LocationNode[] = [
  node(1, "Workshop", "Workshop"),
  node(2, "Cabinet A", "Workshop / Cabinet A"),
  node(3, "07", "Workshop / Cabinet A / 07", { slot_label: "07" }),
  node(4, "Drawer 07 spares", "Workshop / Cabinet B / Drawer 07 spares"),
  node(5, "Cabinet B", "Workshop / Cabinet B"),
];

describe("queryTokens", () => {
  it("splits on whitespace and on the path separator, so a pasted path works", () => {
    expect(queryTokens(" Cabinet A / 07 ")).toEqual(["cabinet", "a", "07"]);
  });

  it("is empty for whitespace, which is what makes an untouched field mean 'browse'", () => {
    expect(queryTokens("   ")).toEqual([]);
  });
});

describe("matchLocations", () => {
  it("matches tokens in any order, with the separators left out", () => {
    const hits = matchLocations(NODES, "cabinet a 07");
    expect(hits.map((hit) => hit.id)).toEqual([3]);
  });

  it("returns nothing for an empty query rather than everything", () => {
    // The picker browses in that case; returning the whole tree would render a
    // flat five-hundred-row list the moment the field was cleared.
    expect(matchLocations(NODES, "")).toEqual([]);
  });

  it("ranks a hit on the container's own name above one that only matched an ancestor", () => {
    const hits = matchLocations(NODES, "07");
    expect(hits[0]?.id).toBe(3);
    expect(hits.map((hit) => hit.id)).toContain(4);
  });

  it("finds a generated cell by its slot label", () => {
    expect(matchLocations(NODES, "07").some((hit) => hit.slot_label === "07")).toBe(true);
  });

  it("puts the shallower container first when scores tie", () => {
    const hits = matchLocations(NODES, "workshop");
    expect(hits[0]?.id).toBe(1);
  });

  it("ignores case", () => {
    expect(matchLocations(NODES, "CABINET b").map((hit) => hit.id)).toContain(5);
  });
});
