/**
 * The recursion claim, asserted rather than intended — ADR 0006.
 *
 * The claim is: **whatever renders a level renders its children by the same
 * rule, at any depth, with no special case for depth 0.** That is easy to say
 * about a component that calls itself and easy to get wrong, because the way it
 * goes wrong is subtle — a hardcoded view at the outer level, a preview that
 * stops honouring the axis and just draws dots, a `depth === 0` branch — and none
 * of those break any other test.
 *
 * So the load-bearing test here renders a **workshop, a cabinet and a baseplate
 * in one pass** and asserts three different drawings appear, nested inside one
 * another in that order. Each of those failure modes turns it red:
 *
 * * hardcode the outer view       → the first `data-view` is wrong
 * * drop the axis in the preview  → the inner two collapse to one kind
 * * stop recursing                → there is no third
 * * special-case depth 0          → the roots stop matching `childViewOf`
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
    // No declared canvas: the positions in this file come from labels alone, which
    // is what every assertion here is about.
    child_grid_rows: null,
    child_grid_cols: null,
    effective_glyph: null,
    is_overfull: false,
    is_staging: false,
    fill_ratio: 0.25,
    lot_count: 1,
    qty_milli: 4000,
  };
}

/**
 * The chain from Iliana's request, verbatim: a workshop, a cabinet in it, a
 * baseplate in a drawer of that cabinet, bins on the baseplate.
 *
 *   Workshop (floor_plan)
 *     └── Cabinet (cabinet_face)
 *           ├── Drawer A1 (grid_cells)
 *           │     ├── Bin A1
 *           │     └── Bin B2
 *           └── Drawer B1 (grid_cells)
 *
 * Three levels, three different pictures, and nothing in the renderer told which
 * is which — the nodes carry it.
 */
const WORKSHOP = [
  node({ id: 1, name: "Workshop", parent_id: null, view: "floor_plan" }),
  node({ id: 2, name: "Cabinet", parent_id: 1, view: "cabinet_face" }),
  node({ id: 3, name: "Drawer A1", parent_id: 2, slot_label: "A1", view: "grid_cells" }),
  node({ id: 4, name: "Drawer B1", parent_id: 2, slot_label: "B1", view: "grid_cells" }),
  node({ id: 5, name: "Bin A1", parent_id: 3, slot_label: "A1", view: "list" }),
  node({ id: 6, name: "Bin B2", parent_id: 3, slot_label: "B2", view: "list" }),
];

function draw(nodes: readonly LocationNode[], parentId: number | null): HTMLElement {
  const index = indexTree(nodes);
  const { container } = render(
    <MemoryRouter>
      <ContainerLayout index={index} parentId={parentId} drillTo={(n) => `/tree?at=${n.id}`} />
    </MemoryRouter>,
  );
  return container;
}

/** Every level drawn in this pass, outermost first. */
function drawnViews(container: HTMLElement): string[] {
  return [...container.querySelectorAll("[data-view]")].map(
    (element) => element.getAttribute("data-view") ?? "",
  );
}

describe("one component, three depths, one pass", () => {
  it("draws a workshop, a cabinet and a baseplate each as itself", () => {
    const container = draw(WORKSHOP, 1);

    // The workshop's own children, then the cabinet's, then the drawer's —
    // three different kinds, from three different nodes, in one render.
    expect(drawnViews(container)).toEqual(["floor_plan", "cabinet_face", "grid_cells"]);
  });

  it("nests them, so each level really is inside the one above", () => {
    // Ordering alone would pass if the three were siblings. Containment is what
    // makes this the recursion rather than a list of coincidences.
    const container = draw(WORKSHOP, 1);
    const [plan, face, cells] = [...container.querySelectorAll("[data-view]")];

    expect(plan).toBeTruthy();
    expect(face).toBeTruthy();
    expect(cells).toBeTruthy();
    expect(plan?.contains(face ?? null)).toBe(true);
    expect(face?.contains(cells ?? null)).toBe(true);
  });

  it("draws each kind with its own geometry, not just its own label", () => {
    const container = draw(WORKSHOP, 1);

    // A floor plan has no rows and columns at all…
    expect(container.querySelector('[data-view="floor_plan"] .layout-plan')).toBeTruthy();
    expect(container.querySelector('[data-view="floor_plan"] .layout-grid')).toBeNull();
    // …while the two slotted kinds are grids, distinguishable from one another
    // by the class that gives one square cells and the other drawer fronts.
    expect(container.querySelector(".mini-cabinet_face")).toBeTruthy();
    expect(container.querySelector(".mini-grid_cells")).toBeTruthy();
  });

  it("says in words which picture each level is, since shape is not readable", () => {
    // The palette's identity hues are a luminance match on purpose, so no view
    // kind may rest on how it looks.
    draw(WORKSHOP, 1);
    expect(screen.getByText(/placed rather than slotted/)).toBeTruthy();
  });
});

describe("depth 0 is not a special case", () => {
  it("draws the roots by the same call, with no container to ask", () => {
    // `parentId === null` has no node to carry a view. It resolves through the
    // identical `childViewOf` call — the world holds furniture — which is also
    // what `resolve_child_view(None, None)` returns on the server.
    const container = draw(WORKSHOP, null);

    expect(drawnViews(container)[0]).toBe("floor_plan");
    expect(screen.getByRole("link", { name: /Workshop/ })).toBeTruthy();
  });

  it("uses the container's own answer the moment there is a container", () => {
    // Same tree, same component, one level in: the only thing that changed is
    // whose `effective_child_view` was asked for.
    const container = draw(WORKSHOP, 2);
    expect(drawnViews(container)[0]).toBe("cabinet_face");
  });
});

describe("the axis is honoured, not the labels", () => {
  it("refuses to draw a grid for a level that is not slotted, even when the labels would allow one", () => {
    // Both children carry perfectly parseable `A1`/`B2` labels. Under the old
    // renderer that alone produced a grid; a floor plan is a statement that
    // there are no positions here, so the labels must not override it.
    const placed = [
      node({ id: 1, name: "Yard", parent_id: null, view: "floor_plan" }),
      node({ id: 2, name: "Shed", parent_id: 1, slot_label: "A1", view: "list" }),
      node({ id: 3, name: "Bench", parent_id: 1, slot_label: "B2", view: "list" }),
    ];
    const container = draw(placed, 1);

    expect(container.querySelector(".layout-grid")).toBeNull();
    expect(container.querySelector(".layout-plan")).toBeTruthy();
    // And no invented empty positions: a yard has no B1 to be empty.
    expect(screen.queryByText("no bin")).toBeNull();
  });

  it("draws the empty positions for a slotted level, because those are real", () => {
    const cabinet = [
      node({ id: 1, name: "Cabinet", parent_id: null, view: "cabinet_face" }),
      node({ id: 2, name: "Drawer A1", parent_id: 1, slot_label: "A1", view: "list" }),
      node({ id: 3, name: "Drawer B2", parent_id: 1, slot_label: "B2", view: "list" }),
    ];
    draw(cabinet, 1);

    expect(screen.getByLabelText("slot A2, no container")).toBeTruthy();
    expect(screen.getByLabelText("slot B1, no container")).toBeTruthy();
  });

  it("says so rather than guessing when a slotted level's labels do not place", () => {
    const cabinet = [
      node({ id: 1, name: "Cabinet", parent_id: null, view: "cabinet_face" }),
      node({ id: 2, name: "Left drawer", parent_id: 1, slot_label: "left", view: "list" }),
      node({ id: 3, name: "Right drawer", parent_id: 1, slot_label: "right", view: "list" }),
    ];
    const container = draw(cabinet, 1);

    expect(container.querySelector(".layout-fallback")).toBeTruthy();
    expect(screen.getByText(/do not fit the row-and-column scheme/)).toBeTruthy();
  });

  it("draws a shelving unit as runs, one per level, rather than as a grid", () => {
    // Rows are authored, columns are not: how many boxes stand on a shelf is a
    // fact about the boxes, so a fixed cell per position would be a false claim
    // about capacity.
    const rack = [
      node({ id: 1, name: "Rack", parent_id: null, view: "shelf_run" }),
      node({ id: 2, name: "Box 1", parent_id: 1, slot_label: "A1", view: "list" }),
      node({ id: 3, name: "Box 2", parent_id: 1, slot_label: "A2", view: "list" }),
      node({ id: 4, name: "Box 3", parent_id: 1, slot_label: "B1", view: "list" }),
    ];
    const container = draw(rack, 1);

    expect(container.querySelectorAll(".layout-shelf")).toHaveLength(2);
    expect(screen.getByRole("group", { name: "shelf 1" }).textContent).toContain("Box 1");
    expect(screen.getByRole("group", { name: "shelf 2" }).textContent).toContain("Box 3");
    expect(container.querySelector(".layout-grid")).toBeNull();
  });

  it("draws a kind it has never heard of as rows instead of not at all", () => {
    // `child_view` carries no CHECK constraint, which is what makes adding a
    // drawing kind a one-line change — and the price is that a newer build can
    // name one this bundle does not know. The server passes such a value through
    // on purpose, so the client owes it a picture.
    const future = [
      node({ id: 1, name: "Something new", parent_id: null, view: "isometric_exploded" }),
      node({ id: 2, name: "Inside it", parent_id: 1, view: "list" }),
    ];
    const container = draw(future, 1);

    expect(drawnViews(container)).toEqual(["list"]);
    expect(screen.getByRole("link", { name: /Inside it/ })).toBeTruthy();
  });
});
