/**
 * What the page behind an editing panel has to re-read when the panel saves.
 *
 * Collapsing every editing task onto one page took away the thing that used to keep
 * this honest: on a separate route, coming back re-mounted the screen and re-fetched
 * everything. A panel over the page does not, so each of the page's three fetches
 * has to be told.
 *
 * `LocationScreen` runs three: the `LocationRead`, the child tree (keyed on
 * `child_count`) and, for a level drawn as a floor plan, the room plan (keyed on
 * `effective_child_view`). Neither of the last two changes its key when a rename, a
 * relabel or a whole rearrangement is saved — so "Inside" kept drawing the old names
 * and the floor plan kept drawing every cabinet where it used to stand. Adding and
 * removing containers appeared to work only because those move `child_count`, which
 * is the kind of accidental correctness that breaks without a test.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { LocationScreen } from "./LocationScreen";

const LOCATION = {
  id: 11,
  parent_id: 2,
  name: "Workbench cabinet",
  slot_label: null,
  short_id: null,
  label_path: "Workshop / Workbench cabinet",
  description: null,
  depth: 1,
  id_path: "/2/11/",
  child_count: 1,
  lot_count: 0,
  qty_milli: 0,
  is_staging: false,
  is_overfull: false,
  is_placeable: true,
  esd_safe: null,
  effective_esd_safe: null,
  child_view: null,
  // Drawn as a flow of cards, so the child's *name* is on screen and a stale
  // tree read is visible rather than merely cached.
  effective_child_view: "list",
  glyph: null,
  effective_glyph: null,
  photo: null,
  effective_photo: null,
  container_type_id: null,
  placement: null,
  access_score: 0.5,
  tare_mg: null,
  display: null,
  last_printed_at: null,
  retired_at: null,
  lots: [],
  capacity: {
    model: "slots",
    used: 0,
    capacity: 30,
    unit: "slots",
    fill_ratio: 0,
    is_full: false,
    is_overfull: false,
  },
};

/** The subtree the real route returns: this container, then what is in it. */
function node(
  id: number,
  parentId: number | null,
  name: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return { ...child(name), id, parent_id: parentId, ...overrides };
}

/** The drawer inside, renamed by the server between the two tree reads. */
function child(name: string): Record<string, unknown> {
  return {
    id: 12,
    parent_id: 11,
    name,
    slot_label: name,
    label_path: `Workshop / Workbench cabinet / ${name}`,
    depth: 2,
    id_path: "/2/11/12/",
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
  };
}

const paths: string[] = [];

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(): void {
  let treeReads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      paths.push(`${request.method} ${url.pathname}`);

      if (url.pathname === "/api/locations/11" && request.method === "GET") {
        return json(LOCATION);
      }
      if (url.pathname === "/api/locations/tree") {
        treeReads += 1;
        // The server's answer changed underneath the page: this is exactly the
        // relabel-in-a-panel case, and `child_count` did not move with it.
        return json({
          nodes: [
            node(11, 2, "Workbench cabinet", { id_path: "/2/11/", depth: 1 }),
            child(treeReads === 1 ? "B2" : "B3"),
          ],
        });
      }
      if (url.pathname === "/api/locations/11/details" && request.method === "PUT") {
        return json({ location: LOCATION, replayed: false });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen(entry: string): void {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/locations/:locationId" element={<LocationScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  paths.length = 0;
  stubApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("re-reads what is inside after a panel saves, not only the container's own row", async () => {
  renderScreen("/locations/11?edit=1&panel=details");

  const dialog = await screen.findByRole("dialog");
  await screen.findByRole("link", { name: /B2/ });

  fireEvent.change(within(dialog).getByLabelText("Name"), {
    target: { value: "Workbench cabinet, west" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

  // `child_count` is unchanged, so the tree's cache key is unchanged: without an
  // explicit reload "Inside" would still be showing B2.
  await waitFor(() => expect(screen.getByRole("link", { name: /B3/ })).toBeTruthy());
  expect(paths.filter((entry) => entry.includes("/api/locations/tree")).length).toBeGreaterThan(1);
});
