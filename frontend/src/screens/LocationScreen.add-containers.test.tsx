/**
 * A container's own screen is the second place somebody looks for "put another
 * drawer in here", so the way to do it has to be on it — and now it *stays* on it.
 *
 * Its own file rather than folded into `LocationScreen.printed-id.test.tsx`: that
 * one is about keeping the printed world and the database in step, and this is about
 * a create path that was reported missing twice. Kept apart, a failure names which
 * of the two broke.
 *
 * The distinction asserted below is the one that matters and is easy to lose:
 * "add containers inside" creates new rows from a container type, while "slots
 * inside" rearranges the positions this container already has and goes through the
 * change guard. One control for both would put a create action behind a guard that
 * exists to protect existing contents.
 *
 * What changed with edit mode: both are **panels over this page**, not links off it.
 * This file used to assert two `<a href>`s — `/containers/new?parent=11` and
 * `/locations/11/layout` — and those were exactly the page-per-editing-task
 * navigations Iliana asked to lose, so the assertions now pin their absence as well
 * as the panels' presence.
 */

import { fireEvent, render, screen, within } from "@testing-library/react";
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
  id_path: "/2/11/",
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

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      if (url.pathname === "/api/locations/11") {
        return json(LOCATION);
      }
      if (url.pathname === "/api/container-types") {
        return json([
          {
            id: 7,
            display_name: "Raaco 15-drawer",
            is_seed: false,
            glyph: null,
            capacity_model: "slots",
            child_view: null,
            effective_child_view: "cabinet_face",
            presents_grid: true,
            occupies_slot: false,
            grid_rows: 3,
            grid_cols: 5,
          },
        ]);
      }
      if (url.pathname === "/api/container-types/7") {
        return json({
          id: 7,
          display_name: "Raaco 15-drawer",
          is_seed: false,
          child_view: null,
          effective_child_view: "cabinet_face",
        });
      }
      if (url.pathname === "/api/locations/11/layout") {
        return json({
          location_id: 11,
          container_type_id: 7,
          grid_rows: 1,
          grid_cols: 1,
          slots: [],
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen(entry = "/locations/11"): void {
  render(
    <MemoryRouter initialEntries={[entry]}>
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
  it("offers it in edit mode, carrying this container as the parent", async () => {
    stubApi();
    renderScreen("/locations/11?edit=1");

    fireEvent.click(await screen.findByRole("button", { name: "Add containers inside…" }));
    const dialog = await screen.findByRole("dialog");
    // The parent is the page you are standing on, so the panel states it rather
    // than asking for it.
    expect(within(dialog).getByText(/These go inside Workshop \/ Workbench cabinet/)).toBeTruthy();
  });

  it("keeps it separate from editing this container's own slots", async () => {
    stubApi();
    renderScreen("/locations/11?edit=1");

    const add = await screen.findByRole("button", { name: "Add containers inside…" });
    const slots = screen.getByRole("button", { name: "Slots inside…" });
    // Two controls, two jobs: one creates rows, the other rearranges the ones that
    // exist and is guarded on their contents.
    expect(add).not.toBe(slots);

    fireEvent.click(slots);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Slots inside this container" })).toBeTruthy();
    expect(within(dialog).getByText(/blocks the change rather than losing it/)).toBeTruthy();
  });

  it("no longer sends anybody to another page to do either", async () => {
    stubApi();
    renderScreen("/locations/11?edit=1");
    await screen.findByText("edit mode");

    // The two routes this replaced. `/locations/11/layout` still exists as a
    // redirect into the panel, but nothing on this page links out to it.
    for (const href of ["/containers/new?parent=11", "/locations/11/layout"]) {
      expect(
        screen.queryAllByRole("link").some((link) => link.getAttribute("href") === href),
      ).toBe(false);
    }
  });
});
