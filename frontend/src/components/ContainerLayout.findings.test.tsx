/**
 * Regressions for what adversarial review found in the container-authoring
 * batch — the three defects that live in the renderer.
 *
 * All three share one shape, and it is the shape worth naming because ADR 0006
 * invites it: **the view axis grew a branch per picture, and each new branch
 * re-derived what to draw from `Layout` instead of drawing what the level
 * actually holds.** A branch that reads `layout.cells` and forgets
 * `layout.unplaced` silently loses containers; a branch that reads
 * `layout.cells` for a view with no positions silently invents them. Neither
 * throws, neither logs, and both look right in a screenshot of a tidy tree.
 *
 * They are collected here rather than filed under `ContainerLayout.recursion`
 * because that file asserts the *claim* (three depths, three drawings, one
 * component) and these assert that the claim did not cost anything: every child
 * is still drawn, and no view draws a position it does not have.
 *
 * 1. `shelf_run` dropped any child whose label was not a grid position.
 * 2. The mini preview drew empty positions for views that have none.
 * 3. A `cabinet_face` whose slot labels are a plain sequence could not be drawn
 *    at all, even though the canvas that promised the face is declared.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { ContainerLayout } from "./ContainerLayout";
import type { LocationNode } from "../lib/api/client";
import { indexTree } from "../lib/locations/tree";

interface Spec {
  id: number;
  name: string;
  parent_id: number | null;
  slot_label?: string | null;
  /** How this node draws **its own** children. */
  view: string;
  /** The canvas this node presents to its children, when it declares one. */
  canvas?: readonly [number, number] | undefined;
}

function node(spec: Spec): LocationNode {
  return {
    id: spec.id,
    name: spec.name,
    parent_id: spec.parent_id,
    depth: 0,
    id_path: `/${spec.id}/`,
    label_path: spec.name,
    container_type_id: null,
    slot_label: spec.slot_label ?? null,
    is_placeable: null,
    effective_child_view: spec.view,
    child_grid_rows: spec.canvas?.[0] ?? null,
    child_grid_cols: spec.canvas?.[1] ?? null,
    effective_glyph: null,
    is_overfull: false,
    is_staging: false,
    fill_ratio: 0.25,
    lot_count: 1,
    qty_milli: 4000,
  };
}

function draw(nodes: readonly LocationNode[], parentId: number | null): HTMLElement {
  const index = indexTree(nodes);
  const { container } = render(
    <MemoryRouter>
      <ContainerLayout index={index} parentId={parentId} drillTo={(n) => `/tree?at=${n.id}`} />
    </MemoryRouter>,
  );
  return container;
}

// ---------------------------------------------------------------------------
// 1. A shelf run silently dropped the containers it could not place
// ---------------------------------------------------------------------------

describe("a shelf run draws every box standing on it", () => {
  /**
   * The defect: `shelf_run` grouped `layout.cells` into rows and never looked at
   * `layout.unplaced`, so a box labelled anything other than a grid position
   * vanished from the DOM entirely.
   *
   * Why the *shape* matters more than the count: in this renderer **the cell is
   * the link**. A child that is not drawn is not merely invisible, it is
   * unreachable — there is no other way into it from the map — and nothing says
   * so. The slotted branch had always handled `layout.unplaced` and the
   * pre-branch renderer had no path that discarded a child at all, so this is a
   * loss introduced by adding a picture, which is exactly the cost ADR 0006's
   * "one branch per drawing" design has to keep paying attention to.
   */
  it("lists a box whose label is not a grid position instead of losing it", () => {
    const rack = [
      node({ id: 1, name: "Rack", parent_id: null, view: "shelf_run" }),
      node({ id: 2, name: "Box A1", parent_id: 1, slot_label: "A1", view: "list" }),
      node({ id: 3, name: "Box A2", parent_id: 1, slot_label: "A2", view: "list" }),
      node({ id: 4, name: "Box B1", parent_id: 1, slot_label: "B1", view: "list" }),
      node({ id: 5, name: "Spare box", parent_id: 1, slot_label: "spare", view: "list" }),
    ];
    const container = draw(rack, 1);

    expect(container.querySelectorAll(".layout-shelf")).toHaveLength(2);
    // Every one of the four, and the unplaced one reachable as a link like the
    // rest — not merely present as text somewhere.
    for (const name of ["Box A1", "Box A2", "Box B1", "Spare box"]) {
      expect(screen.getByRole("link", { name: new RegExp(name) })).toBeTruthy();
    }
    // And it says why that one is not on a shelf, rather than placing it on a
    // guessed one.
    expect(screen.getByText(/not a grid position/)).toBeTruthy();
  });

  it("still draws everything when no label places at all", () => {
    // The mixed case above hides behind `kind === "grid"`; this one takes the
    // other branch, and both have to draw four boxes.
    const rack = [
      node({ id: 1, name: "Rack", parent_id: null, view: "shelf_run" }),
      node({ id: 2, name: "Left", parent_id: 1, slot_label: "left", view: "list" }),
      node({ id: 3, name: "Middle", parent_id: 1, slot_label: "middle", view: "list" }),
      node({ id: 4, name: "Right", parent_id: 1, slot_label: "right", view: "list" }),
    ];
    draw(rack, 1);

    for (const name of ["Left", "Middle", "Right"]) {
      expect(screen.getByRole("link", { name: new RegExp(name) })).toBeTruthy();
    }
  });
});

// ---------------------------------------------------------------------------
// 2. The preview invented positions the full drawing refuses to
// ---------------------------------------------------------------------------

describe("a preview draws the same positions the full drawing would", () => {
  /**
   * The defect: `MiniLayout` built its dots from `layout.cells` for every view,
   * and `cells` includes the empty positions of an inferred grid. So a workshop
   * holding "Shed A1" and "Shed B2" previewed as four dots, two of them blank,
   * in four columns — while the full drawing of that same workshop one click
   * away is a floor plan of two cards with no empty position anywhere.
   *
   * Why the shape matters: `isSlotted` is documented as *the* one behavioural
   * consequence of the view axis — only a cabinet face and a grid of cells draw
   * the positions that are empty, because "a room has no position to be empty,
   * so inventing one there would be inventing furniture". The preview reached
   * the column count but not that rule, so the map contradicted itself about
   * the same level at two zoom levels. A blank dot in a floor plan is a claim
   * that a shelf is missing from a room.
   */
  it("shows a floor plan's preview as one dot per thing, with no blanks", () => {
    const site = [
      node({ id: 9, name: "Site", parent_id: null, view: "floor_plan" }),
      node({ id: 10, name: "Workshop", parent_id: 9, view: "floor_plan" }),
      node({ id: 11, name: "Shed A1", parent_id: 10, slot_label: "A1", view: "list" }),
      node({ id: 12, name: "Shed B2", parent_id: 10, slot_label: "B2", view: "list" }),
    ];
    const container = draw(site, 9);

    const preview = container.querySelector('.mini-grid[data-view="floor_plan"]');
    expect(preview).toBeTruthy();
    expect(preview?.querySelectorAll(".mini-cell")).toHaveLength(2);
    expect(preview?.querySelectorAll(".mini-cell.blank")).toHaveLength(0);
  });

  it("shows a list's preview the same way — an order has no empty position", () => {
    const bin = [
      node({ id: 1, name: "Baseplate", parent_id: null, view: "grid_cells" }),
      node({ id: 2, name: "Bin A1", parent_id: 1, slot_label: "A1", view: "list" }),
      node({ id: 3, name: "Divider A1", parent_id: 2, slot_label: "A1", view: "list" }),
      node({ id: 4, name: "Divider B2", parent_id: 2, slot_label: "B2", view: "list" }),
    ];
    const container = draw(bin, 1);

    const preview = container.querySelector('.mini-grid[data-view="list"]');
    expect(preview?.querySelectorAll(".mini-cell")).toHaveLength(2);
    expect(preview?.querySelectorAll(".mini-cell.blank")).toHaveLength(0);
  });

  it("keeps drawing the blanks where they are real, so this is not a blanket removal", () => {
    // The other half of the same rule: a cabinet face's empty drawer is a fact
    // about the furniture and has to survive.
    const site = [
      node({ id: 1, name: "Room", parent_id: null, view: "floor_plan" }),
      node({ id: 2, name: "Cabinet", parent_id: 1, view: "cabinet_face" }),
      node({ id: 3, name: "Drawer A1", parent_id: 2, slot_label: "A1", view: "list" }),
      node({ id: 4, name: "Drawer B2", parent_id: 2, slot_label: "B2", view: "list" }),
    ];
    const container = draw(site, 1);

    const preview = container.querySelector('.mini-grid[data-view="cabinet_face"]');
    expect(preview?.querySelectorAll(".mini-cell")).toHaveLength(4);
    expect(preview?.querySelectorAll(".mini-cell.blank")).toHaveLength(2);
  });

  it("previews an unplaceable child too, rather than dropping it as the shelf did", () => {
    const site = [
      node({ id: 1, name: "Room", parent_id: null, view: "floor_plan" }),
      node({ id: 2, name: "Cabinet", parent_id: 1, view: "cabinet_face" }),
      node({ id: 3, name: "Drawer A1", parent_id: 2, slot_label: "A1", view: "list" }),
      node({ id: 4, name: "Odd drawer", parent_id: 2, slot_label: "spare", view: "list" }),
    ];
    const container = draw(site, 1);

    // One placed cell and one unplaced child: two dots, no blank.
    const preview = container.querySelector('.mini-grid[data-view="cabinet_face"]');
    expect(preview?.querySelectorAll(".mini-cell")).toHaveLength(2);
  });
});

// ---------------------------------------------------------------------------
// 3. The cabinet face the derivation promised, but could not draw
// ---------------------------------------------------------------------------

describe("a cabinet face whose labels are a plain sequence", () => {
  /**
   * The defect, end to end: `POST /api/locations/{id}/instantiate` with
   * `raaco-c8-30` produces 30 children labelled `01`…`30`, and
   * `derive_child_view` reads that type's 30x1 canvas as `cabinet_face` — ADR
   * 0006 says so in as many words. But a plain-numbered label carries no column,
   * so `inferLayout` returned `kind: "sequence"`, the slotted branch was skipped
   * and the level fell through to the fallback: no grid, and a note reading
   * "an order, but no rows and columns to place them in".
   *
   * Why the shape matters: the backend *derived a promise from geometry the
   * client was never given*. Both halves read the same two columns
   * (`grid_rows`/`grid_cols`) — one to decide the picture, the other to draw it —
   * and only one of them could see them, so two of the eleven seed types
   * declared a drawing that was unreachable by construction. Fixed by carrying
   * the canvas on `LocationNode` rather than by weakening the derivation: the
   * fact that promises the face is the fact that makes it drawable.
   *
   * Note the positions here are still *authored*, not guessed. A sequential
   * label's index maps to `(index / cols, index % cols)` — exactly the row-major
   * enumeration `layout_authoring.generate_label` used to mint it — so nothing
   * is inferred that the label and the declared canvas do not already state.
   */
  const raaco = [
    node({ id: 1, name: "Room", parent_id: null, view: "floor_plan" }),
    node({ id: 2, name: "Raaco C8-30", parent_id: 1, view: "cabinet_face", canvas: [30, 1] }),
    ...Array.from({ length: 5 }, (_, i) =>
      node({
        id: 10 + i,
        name: `Drawer ${i + 1}`,
        parent_id: 2,
        slot_label: String(i + 1).padStart(2, "0"),
        view: "list",
      }),
    ),
  ];

  it("draws it as a face, not as a fallback flow", () => {
    const container = draw(raaco, 2);

    const grid = container.querySelector(".layout-grid.view-cabinet_face");
    expect(grid).toBeTruthy();
    expect(container.querySelector(".layout-fallback")).toBeNull();
    expect(screen.queryByText(/no rows and columns to place them in/)).toBeNull();
  });

  it("puts one drawer per row, because the canvas says one column", () => {
    const container = draw(raaco, 2);
    const grid = container.querySelector(".layout-grid.view-cabinet_face");

    expect(grid?.getAttribute("style")).toContain("repeat(1,");
    for (const [i, cell] of [...(grid?.children ?? [])].entries()) {
      expect(cell.getAttribute("style")).toContain(`grid-row: ${i + 1}`);
    }
  });

  it("prints the label the drawer actually carries, zero padding and all", () => {
    // `slots.ts` used to relabel a sequence from its own index, so a drawer
    // whose real card reads `01` was drawn as `1` — the screen no longer
    // matching the furniture, which is the whole reason positions are never
    // guessed here.
    draw(raaco, 2);
    expect(screen.getByText("01")).toBeTruthy();
    expect(screen.getByText("05")).toBeTruthy();
  });

  it("still refuses to draw a face when nothing declares a canvas", () => {
    // The narrowness is the point: a sequence with no declared canvas is still
    // an order with no geometry, and guessing one column for it would be
    // inventing the cabinet.
    const undeclared = [
      node({ id: 1, name: "Room", parent_id: null, view: "floor_plan" }),
      node({ id: 2, name: "Mystery cabinet", parent_id: 1, view: "cabinet_face" }),
      node({ id: 3, name: "Drawer 1", parent_id: 2, slot_label: "1", view: "list" }),
      node({ id: 4, name: "Drawer 2", parent_id: 2, slot_label: "2", view: "list" }),
    ];
    const container = draw(undeclared, 2);

    expect(container.querySelector(".layout-grid")).toBeNull();
    expect(screen.getByText(/no rows and columns to place them in/)).toBeTruthy();
  });
});
