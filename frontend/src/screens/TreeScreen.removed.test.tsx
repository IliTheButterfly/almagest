/**
 * Where a removed container can still be found.
 *
 * Removing a container the ledger, a printed label or a tag names *retires* it: the
 * row and all its history stay, and the container leaves the tree. The panel that
 * does it says "it can be restored" — and restoring happens on the container's own
 * page.
 *
 * Which is a promise the UI did not keep, because retirement takes the row out of
 * every read: it is in no parent's children, no slot canvas, no room plan and no
 * assignment proposal. `GET /api/locations/tree?include_retired=true` existed for
 * "the one screen that offers to restore them" and nothing ever sent it, so the only
 * way back to a removed drawer was to hand-type its numeric id into the URL or to
 * scan the tag still stuck to its front. This is that screen.
 *
 * Asked for, not fetched by default: the extra read exists for a rare intention, and
 * every ordinary tree read must stay one request.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { TreeScreen } from "./TreeScreen";

function node(
  id: number,
  name: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id,
    name,
    parent_id: null,
    depth: 0,
    id_path: `/${id}/`,
    label_path: name,
    container_type_id: null,
    slot_label: null,
    is_placeable: true,
    effective_child_view: "floor_plan",
    effective_glyph: null,
    is_overfull: false,
    is_staging: false,
    fill_ratio: null,
    lot_count: 0,
    qty_milli: 0,
    child_grid_rows: null,
    child_grid_cols: null,
    retired_at: null,
    ...overrides,
  };
}

const LIVE = node(1, "Cabinet A");
const RETIRED = node(3, "Drawer B3", {
  parent_id: 1,
  depth: 1,
  id_path: "/1/3/",
  label_path: "Cabinet A / Drawer B3",
  retired_at: "2026-07-28T10:00:00Z",
});

const queries: string[] = [];

function stubTree(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      // The map is a master/detail workspace now, so the level in view is also
      // read on its own for the panel beside it.
      if (/^\/api\/locations\/\d+$/.test(url.pathname)) {
        return new Response(
          JSON.stringify({
            ...LIVE,
            description: null,
            short_id: null,
            effective_esd_safe: null,
            child_count: 0,
            lots: [],
            capacity: { model: "none", unit: "none", used: 0, capacity: null, fill_ratio: null, is_overfull: false },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.pathname !== "/api/locations/tree") {
        throw new Error(`unstubbed request: ${url.pathname}`);
      }
      queries.push(url.search);
      const withRetired = url.searchParams.get("include_retired") === "true";
      return new Response(JSON.stringify({ nodes: withRetired ? [LIVE, RETIRED] : [LIVE] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

function renderTree(): void {
  render(
    <MemoryRouter initialEntries={["/tree"]}>
      <Routes>
        <Route path="/tree" element={<TreeScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  queries.length = 0;
  stubTree();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("lists a removed container, and links to the page that brings it back", async () => {
  renderTree();
  await screen.findByRole("button", { name: /^Show: Cabinet A/ });

  // Not asked for yet, so not fetched: the ordinary tree read stays one request.
  expect(queries.every((search) => !search.includes("include_retired"))).toBe(true);
  expect(screen.queryByText(/Cabinet A \/ Drawer B3/)).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Show them" }));

  const link = await screen.findByRole("link", { name: /Drawer B3/ });
  expect(link.getAttribute("href")).toBe("/locations/3");
  // The path, because the name alone ("B3") does not say which cabinet it left.
  expect(screen.getByText("Cabinet A / Drawer B3")).toBeTruthy();
  await waitFor(() =>
    expect(queries.some((search) => search.includes("include_retired=true"))).toBe(true),
  );
});

it("says a removed container is removed in a word, not by where it sits", async () => {
  renderTree();
  fireEvent.click(await screen.findByRole("button", { name: "Show them" }));
  const link = await screen.findByRole("link", { name: /Drawer B3/ });
  expect(link.textContent).toContain("removed");
});
