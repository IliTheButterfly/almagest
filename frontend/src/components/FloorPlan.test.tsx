/**
 * Reading a drawn room — the payoff, and the half that has to be honest.
 *
 * Mounted through `ContainerLayout` rather than in isolation, because the claim
 * being made is "a level drawn as a floor plan draws the plan the server holds",
 * and only the renderer that *chooses* the picture can say that. Which also puts
 * the recursion rule under test: `childViewOf(index, parentId)` is the single call
 * that decides, at every depth, so the last test renders the identical plan at
 * depth 0 and depth 3 and demands identical output. A `depth === 0` branch would
 * pass every other test in this file.
 *
 * What read mode owes:
 *
 * - **The plan, not a flow of cards.** Positions here are authored millimetres,
 *   unlike the two slotted views whose geometry is read back out of label text.
 * - **No interaction.** Nothing drags, rotates or saves; every box is a link,
 *   because in this renderer the box *is* the way to reach the container, and a
 *   container that cannot be reached from the map is lost rather than merely
 *   undrawn.
 * - **The tray, always.** An unplaced child is listed as unplaced. It is never
 *   drawn at the origin, which is what ADR 0009 refuses to default to.
 * - **A scale.** A plan with no scale is a doodle.
 */

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";

import { ContainerLayout } from "./ContainerLayout";
import type { LocationNode, RoomPlanRead } from "../lib/api/client";
import { indexTree } from "../lib/locations/tree";
import { LocationScreen } from "../screens/LocationScreen";

function node(overrides: Partial<LocationNode> & { id: number; name: string }): LocationNode {
  return {
    parent_id: 11,
    slot_label: null,
    label_path: `Workshop / ${overrides.name}`,
    depth: 1,
    id_path: `/11/${overrides.id}/`,
    lot_count: 0,
    qty_milli: 0,
    fill_ratio: null,
    is_overfull: false,
    is_staging: false,
    is_placeable: true,
    container_type_id: null,
    effective_child_view: "list",
    effective_glyph: null,
    child_grid_rows: null,
    child_grid_cols: null,
    retired_at: null,
    ...overrides,
  } as LocationNode;
}

/**
 * A room at whatever depth the caller wants, drawn as a floor plan.
 *
 * **`parent_id` and `id_path` vary with the depth, not just `depth` itself.** A
 * fixture that only moved the `depth` number left node 11 a root — `parent_id:
 * null`, `id_path: "/11/"` — in both runs, so the depth-invariance test could only
 * catch a literal `depth === 0` branch and would have passed a renderer that asked
 * "am I at the top?" the natural way. That is the more likely shortcut, and
 * `views.ts` claims neither is there.
 */
function tree(depth: number): LocationNode[] {
  const deep = depth > 0;
  // Ancestors the fixture does not otherwise need, purely so the room is genuinely
  // *not* a root when it claims not to be.
  const path = deep ? `/${Array.from({ length: depth }, (_, at) => at + 90).join("/")}/11/` : "/11/";
  return [
    node({
      id: 11,
      name: "Workshop",
      parent_id: deep ? 90 : null,
      depth,
      id_path: path,
      effective_child_view: "floor_plan",
    }),
    node({ id: 12, name: "Bench", depth: depth + 1, id_path: `${path}12/` }),
    node({ id: 13, name: "Steel shelf", depth: depth + 1, id_path: `${path}13/` }),
    node({ id: 14, name: "Loose box", depth: depth + 1, id_path: `${path}14/` }),
  ];
}

function plan(overrides: Partial<RoomPlanRead> = {}): RoomPlanRead {
  return {
    location_id: 11,
    shapes: [
      {
        id: 1,
        kind: "outline",
        label: "Workshop",
        is_closed: true,
        thickness_mm: 100,
        sort_order: 0,
        points: [
          { x_mm: 0, y_mm: 0 },
          { x_mm: 5000, y_mm: 0 },
          { x_mm: 5000, y_mm: 4000 },
          { x_mm: 0, y_mm: 4000 },
        ],
      },
    ],
    placements: [
      {
        location_id: 12,
        parent_id: 11,
        x_mm: 200,
        y_mm: 3400,
        rotation_deg: 0,
        width_mm: 1800,
        depth_mm: 600,
        own_width_mm: 1800,
        own_depth_mm: 600,
      },
      {
        location_id: 13,
        parent_id: 11,
        x_mm: 4200,
        y_mm: 500,
        rotation_deg: 90,
        // Unmeasured: drawn at a nominal size, dashed, and said out loud.
        width_mm: null,
        depth_mm: null,
        own_width_mm: null,
        own_depth_mm: null,
      },
    ],
    unplaced_location_ids: [14],
    extent: { min_x_mm: 0, min_y_mm: 0, max_x_mm: 5000, max_y_mm: 4000 },
    ...overrides,
  };
}

function renderPlan(nodes: LocationNode[], roomPlan: RoomPlanRead | null): void {
  render(
    <MemoryRouter>
      <ContainerLayout
        index={indexTree(nodes)}
        parentId={11}
        drillTo={(child) => `/tree?at=${child.id}`}
        plan={roomPlan}
      />
    </MemoryRouter>,
  );
}

function surface(): HTMLElement {
  return screen.getByRole("group", { name: "Floor plan" });
}

it("draws the room and every container standing in it, to scale", () => {
  renderPlan(tree(0), plan());

  const drawn = within(surface());
  const bench = drawn.getByRole("link", { name: /^Bench, at / });
  // The coordinate and the size are in the accessible name, because the picture is
  // `aria-hidden` ink and a screen reader gets nothing from a `<polygon>`.
  expect(bench.getAttribute("aria-label")).toBe(
    "Bench, at 200 mm across, 3.4 m down, 1.8 m by 600 mm",
  );
  // Percentages of the frame, never pixels: the same plan is read on a phone and on
  // a desktop and neither is told a size.
  expect(bench.getAttribute("style")).toContain("%");

  const shelf = drawn.getByRole("link", { name: /^Steel shelf, at / });
  // Unmeasured footprints say so in the name and in the border, not by hue alone.
  expect(shelf.getAttribute("aria-label")).toContain("(nominal)");
  expect(shelf.className).toContain("plan-box-nominal");
  expect(shelf.getAttribute("aria-label")).toContain("turned 90 degrees");
  expect(shelf.getAttribute("style")).toContain("rotate(90deg)");

  // A plan with no scale is a doodle.
  expect(screen.getByText(/grid /)).toBeTruthy();
});

it("is a plan to read, not to edit: nothing in it is a control", () => {
  renderPlan(tree(0), plan());

  // Every box is a link — the box *is* how the container is reached — and there is
  // nothing to drag, rotate or save. Editing lives in edit mode, behind its own
  // deliberate toggle, because rearranging furniture and using storage are
  // different intentions and a mis-tap between them is expensive.
  expect(within(surface()).queryAllByRole("button")).toEqual([]);
  expect(within(surface()).getAllByRole("link")).toHaveLength(2);
  expect(screen.queryByRole("button", { name: /Save/ })).toBeNull();
});

it("says which containers are not placed instead of standing them in the corner", () => {
  renderPlan(tree(0), plan());

  // Not drawn at (0, 0): a defaulted coordinate would put every unplaced container
  // in the same corner of every room and look authored.
  expect(within(surface()).queryByRole("link", { name: /^Loose box, at / })).toBeNull();
  expect(screen.getByText(/Not placed yet \(1\)/)).toBeTruthy();
  // And it is still reachable, drawn by the same card the flow view uses.
  expect(screen.getByRole("link", { name: /Loose box/ })).toBeTruthy();
});

it("falls back to the flow of cards when nobody has drawn anything", () => {
  // An undrawn room is not an empty picture: it is the old honest one, "listed in
  // the order they were added", because no plan means nobody has said where these
  // are — the same refusal `slots.ts` makes about a guessed grid.
  renderPlan(tree(0), { ...plan(), shapes: [], placements: [], unplaced_location_ids: [12, 13, 14] });

  expect(screen.queryByRole("group", { name: "Floor plan" })).toBeNull();
  expect(screen.getByText(/Listed in the order they were added/)).toBeTruthy();
  expect(screen.getAllByRole("link")).toHaveLength(3);
});

it("is what the container's own page shows, without being asked to edit anything", async () => {
  // The wiring, which the component tests above cannot see: the page fetches the
  // plan on the strength of *this level's* `effective_child_view` — the one question
  // ADR 0006 says decides the picture — and draws it in normal mode. That is the
  // whole point of the coordinates: the workshop looks like the workshop before
  // anybody presses Edit.
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url, "http://localhost");
      paths.push(url.pathname);
      const body = (): Response =>
        new Response(JSON.stringify(page(url.pathname)), {
          headers: { "content-type": "application/json" },
        });
      return body();
    }),
  );

  function page(pathname: string): unknown {
    if (pathname === "/api/locations/tree") {
      return { nodes: tree(0) };
    }
    if (pathname === "/api/locations/11/plan") {
      return plan();
    }
    return {
      ...tree(0)[0],
      short_id: null,
      display: null,
      description: null,
      child_count: 3,
      esd_safe: null,
      effective_esd_safe: null,
      child_view: null,
      glyph: null,
      photo: null,
      effective_photo: null,
      placement: null,
      access_score: 0.5,
      tare_mg: null,
      last_printed_at: null,
      retired_at: null,
      lots: [],
      capacity: {
        model: "none",
        used: 0,
        capacity: null,
        unit: "containers",
        fill_ratio: null,
        is_full: false,
        is_overfull: false,
      },
    };
  }

  render(
    <MemoryRouter initialEntries={["/locations/11"]}>
      <Routes>
        <Route path="/locations/:locationId" element={<LocationScreen />} />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("group", { name: "Floor plan" })).toBeTruthy();
  expect(paths).toContain("/api/locations/11/plan");
  expect(within(surface()).getByRole("link", { name: /^Bench, at / })).toBeTruthy();
  // Read mode: no edit affordance has been opened, and the plan is still drawn.
  expect(screen.queryByText("edit mode")).toBeNull();
  vi.unstubAllGlobals();
});

it("draws the identical plan at every depth", () => {
  // The recursion claim. The room's own `effective_child_view` is the only thing
  // that decides, asked with that level's own id, so where it sits in the tree
  // cannot change the picture.
  const names: string[][] = [];
  for (const depth of [0, 3]) {
    const view = render(
      <MemoryRouter>
        <ContainerLayout
          index={indexTree(tree(depth))}
          parentId={11}
          drillTo={(child) => `/tree?at=${child.id}`}
          plan={plan()}
        />
      </MemoryRouter>,
    );
    names.push(
      within(screen.getByRole("group", { name: "Floor plan" }))
        .getAllByRole("link")
        .map((link) => link.getAttribute("aria-label") ?? ""),
    );
    view.unmount();
  }
  const [top, deep] = names;
  expect(top?.length).toBe(2);
  expect(deep).toEqual(top);
});
