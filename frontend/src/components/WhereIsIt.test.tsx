/**
 * "Where is it?" — the claims that make it better than the string it replaces.
 *
 * The panel is easy to make *look* right and hard to keep honest, and the two
 * ways it goes wrong are the two tests that matter here:
 *
 * * **The picture must be the map's picture.** It draws by handing levels to
 *   `ContainerLayout`, so a cabinet face draws its empty slots too — and "the
 *   second of four drawers" is only legible against the other three. A panel that
 *   quietly drew only the containers on the route would look tidier and teach
 *   nothing.
 * * **It must never invent a route.** A destination outside the tree falls back
 *   to the plain path, out loud. This is the failure mode with teeth: a confident
 *   drawing of the wrong cabinet is worse than the grey line of text that was
 *   there before.
 *
 * The third test is the reason the feature is bearable on a phone: a level with
 * nothing to choose between is a sentence, not a map.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WhereIsIt } from "./WhereIsIt";

interface Spec {
  id: number;
  name: string;
  parent_id: number | null;
  slot_label?: string | null;
  view?: string;
}

function node(spec: Spec): Record<string, unknown> {
  return {
    id: spec.id,
    name: spec.name,
    parent_id: spec.parent_id,
    depth: 0,
    id_path: `/${spec.id}/`,
    label_path: spec.name,
    container_type_id: null,
    slot_label: spec.slot_label ?? null,
    is_placeable: true,
    effective_child_view: spec.view ?? "list",
    child_grid_rows: null,
    child_grid_cols: null,
    effective_glyph: null,
    is_overfull: false,
    is_staging: false,
    fill_ratio: 0.2,
    lot_count: 1,
    qty_milli: 1000,
    retired_at: null,
  };
}

/**
 * A workshop and a shed; one cabinet of four drawers; the part is in the second.
 *
 * Two roots so the first turn has something to choose between, and four drawers
 * on a cabinet face so the drawing has a haystack in it. The cabinet is the only
 * container in the workshop *on purpose* — that is the level the panel is
 * supposed to decline to draw.
 */
const NODES = [
  node({ id: 1, name: "Workshop", parent_id: null, view: "list" }),
  node({ id: 2, name: "Shed", parent_id: null }),
  node({ id: 10, name: "Cabinet A", parent_id: 1, view: "cabinet_face" }),
  node({ id: 20, name: "Drawer A1", parent_id: 10, slot_label: "A1" }),
  node({ id: 21, name: "Drawer A2", parent_id: 10, slot_label: "A2" }),
  node({ id: 22, name: "Drawer B1", parent_id: 10, slot_label: "B1" }),
  node({ id: 23, name: "Drawer B2", parent_id: 10, slot_label: "B2" }),
];

function stubTree(nodes: readonly Record<string, unknown>[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.pathname === "/api/locations/tree") {
        return json({ nodes });
      }
      // A room-plan fetch is legitimate for a `floor_plan` level; an empty plan
      // is the honest "nobody has drawn this room" and falls through to cards.
      if (url.pathname.endsWith("/plan")) {
        return json({ shapes: [], placements: [], unplaced_location_ids: [], extent: null });
      }
      throw new Error(`unstubbed ${url.pathname}`);
    }),
  );
}

function mount(locationId: number, labelPath: string) {
  return render(
    <MemoryRouter>
      <WhereIsIt locationId={locationId} labelPath={labelPath} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WhereIsIt", () => {
  it("draws the level the drawer is on with its empty siblings, and marks the one", async () => {
    stubTree(NODES);
    mount(21, "Workshop / Cabinet A / Drawer A2");

    // The whole answer in one line first — this is what a screen reader gets
    // instead of the drawings.
    expect(await screen.findByText("Workshop, then Cabinet A, then Drawer A2")).toBeTruthy();

    const steps = await screen.findAllByRole("listitem");
    const cabinetStep = steps.find((step) => step.textContent?.includes("go to Drawer A2"));
    expect(cabinetStep).toBeTruthy();

    // The drawing itself, not the sentence above it — the step heading names the
    // target too, and it is the *picture* that has to hold the haystack.
    const drawn = (cabinetStep as HTMLElement).querySelector(".where-step-map");
    expect(drawn).toBeTruthy();

    // All four drawer fronts are drawn, not just the one on the route: the
    // marked cell is only findable against the ones it is not.
    for (const name of ["Drawer A1", "Drawer A2", "Drawer B1", "Drawer B2"]) {
      expect(within(drawn as HTMLElement).getByText(name)).toBeTruthy();
    }

    // …and exactly one of them is flagged, in words rather than only in colour.
    expect(within(drawn as HTMLElement).getAllByText("this one").length).toBe(1);
    expect(
      within(drawn as HTMLElement).getByRole("link", { name: /^This one: Drawer A2/ }),
    ).toBeTruthy();
  });

  it("says a level with nothing to choose between rather than drawing it", async () => {
    stubTree(NODES);
    mount(21, "Workshop / Cabinet A / Drawer A2");

    const steps = await screen.findAllByRole("listitem");
    const workshopStep = steps.find((step) => step.textContent?.includes("go to Cabinet A"));
    expect(workshopStep).toBeTruthy();

    // The workshop holds only the cabinet, so there is no haystack and no map —
    // and no count, because "one of 1" is not a fact worth a phone screen.
    expect(workshopStep?.textContent).not.toContain("one of");
    expect(within(workshopStep as HTMLElement).queryByText("Drawer A1")).toBeNull();

    // The top level does have a choice, so it keeps both the count and a picture.
    const topStep = steps.find((step) => step.textContent?.includes("Start at"));
    expect(topStep?.textContent).toContain("one of 2");
    expect(within(topStep as HTMLElement).getByText("Shed")).toBeTruthy();
  });

  it("falls back to the plain path, out loud, when the tree does not hold it", async () => {
    stubTree(NODES);
    mount(999, "Somewhere / Retired shelf");

    expect(await screen.findByText("Somewhere / Retired shelf")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByText(/not in the current map of storage/)).toBeTruthy();
    });
    // Nothing is drawn — no route is better than a wrong one.
    expect(screen.queryByText("this one")).toBeNull();
  });
});
