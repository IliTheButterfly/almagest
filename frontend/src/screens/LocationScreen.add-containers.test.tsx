/**
 * A container's own screen is the second place somebody looks for "put another
 * drawer in here", so the route to do it has to be on it.
 *
 * Its own file rather than folded into `LocationScreen.printed-id.test.tsx`: that
 * one is about keeping the printed world and the database in step, and this is
 * about a create path that was reported missing twice. Kept apart, a failure names
 * which of the two broke.
 *
 * The distinction asserted below is the one that matters and is easy to lose:
 * "add containers inside" creates new rows from a container type, while "edit
 * layout" rearranges the slots this container already has and goes through the
 * change guard. One button for both would put a create action behind a guard that
 * exists to protect existing contents.
 */

import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocationScreen } from "./LocationScreen";

const LOCATION = {
  id: 11,
  parent_id: 2,
  name: "Workbench cabinet",
  slot_label: null,
  short_id: "4K7T92M8",
  label_path: "Workshop / Workbench cabinet",
  description: null,
  depth: 1,
  child_count: 0,
  lot_count: 0,
  qty_milli: 0,
  is_staging: false,
  is_overfull: false,
  is_placeable: true,
  esd_safe: null,
  effective_esd_safe: null,
  child_view: null,
  effective_child_view: "cabinet_face",
  glyph: null,
  effective_glyph: null,
  photo: null,
  effective_photo: null,
  container_type_id: 7,
  access_score: 0.5,
  tare_mg: null,
  display: null,
  last_printed_at: null,
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

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname === "/api/locations/11") {
        return new Response(JSON.stringify(LOCATION), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen(): void {
  render(
    <MemoryRouter initialEntries={["/locations/11"]}>
      <Routes>
        <Route path="/locations/:locationId" element={<LocationScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("adding containers from a container's own screen", () => {
  it("offers it, carrying this container as the parent", async () => {
    stubApi();
    renderScreen();
    const add = await screen.findByRole("link", { name: /Add containers inside/ });
    expect(add.getAttribute("href")).toBe("/containers/new?parent=11");
  });

  it("keeps it separate from editing this container's own layout", async () => {
    stubApi();
    renderScreen();
    const edit = await screen.findByRole("link", { name: /Edit layout/ });
    expect(edit.getAttribute("href")).toBe("/locations/11/layout");
    // Two links, two jobs: one creates rows, the other rearranges the ones that
    // exist and is guarded on their contents.
    expect(screen.getByRole("link", { name: /Add containers inside/ })).not.toBe(edit);
  });
});
