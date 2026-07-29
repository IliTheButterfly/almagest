/**
 * A project's staging box is not the INBOX, and the storage map has to say so.
 *
 * **The defect was an actively false sentence, not a missing one.** ADR 0004 gives
 * every project a lazily-created box carrying the same `is_staging` flag the INBOX
 * has, and both screens keyed off that flag alone. So a box deliberately holding a
 * board's parts rendered the bare "inbox" badge and — worse — the tree screen's
 * hardcoded notice told the reader it "is meant to be emptied rather than lived
 * in". That is advice to undo a withdrawal somebody meant to make, which is the
 * opposite of what the box is for.
 *
 * The discriminator under test is `is_placeable === false`, and the reason it is
 * that rather than a `label_path` prefix match on `"PROJECTS"` is worth restating:
 * `staging.py` sets the flag explicitly on every project box so auto-assignment
 * can never propose one as a home, and `capacity.get_inbox_location` finds the
 * INBOX with the same predicate inverted. The root is an ordinary location and
 * therefore renameable, so any test on its name would start lying the day someone
 * renames it.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LocationScreen } from "./LocationScreen";
import { TreeScreen } from "./TreeScreen";

interface NodeSpec {
  id: number;
  name: string;
  parent_id: number | null;
  is_staging?: boolean;
  is_placeable?: boolean | null;
}

function node(spec: NodeSpec): Record<string, unknown> {
  return {
    id: spec.id,
    name: spec.name,
    parent_id: spec.parent_id,
    depth: spec.parent_id === null ? 0 : 1,
    id_path: spec.parent_id === null ? `/${spec.id}/` : `/${spec.parent_id}/${spec.id}/`,
    label_path: spec.name,
    container_type_id: null,
    slot_label: null,
    is_overfull: false,
    is_staging: spec.is_staging ?? false,
    is_placeable: spec.is_placeable ?? null,
    // Always resolved by the API (ADR 0006). Nothing here turns on it — these
    // are staging *kinds* — but the shape has to stay the wire shape.
    effective_child_view: "floor_plan",
    effective_glyph: null,
    fill_ratio: null,
    lot_count: 1,
    qty_milli: 3_000,
  };
}

/**
 * Both staging kinds side by side, differing **only** in `is_placeable` — the
 * INBOX leaves it null (inherit), a project box states `false`. Shaped exactly
 * like `services/staging.py`'s output, right down to the box being named for the
 * project rather than for the tree position.
 */
const NODES = [
  node({ id: 5, name: "INBOX", parent_id: null, is_staging: true }),
  node({ id: 6, name: "PROJECTS", parent_id: null, is_staging: true, is_placeable: false }),
  node({
    id: 7,
    name: "Blinky v2",
    parent_id: 6,
    is_staging: true,
    is_placeable: false,
  }),
];

function locationRead(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 7,
    name: "Blinky v2",
    parent_id: 6,
    depth: 1,
    id_path: "/6/7/",
    label_path: "PROJECTS / Blinky v2",
    container_type_id: null,
    description: null,
    slot_label: "P3",
    esd_safe: null,
    effective_esd_safe: null,
    is_placeable: false,
    child_view: null,
    effective_child_view: "floor_plan",
    glyph: null,
    effective_glyph: null,
    photo: null,
    effective_photo: null,
    is_overfull: false,
    is_staging: true,
    access_score: 0.5,
    tare_mg: null,
    short_id: null,
    display: null,
    child_count: 0,
    last_printed_at: null,
    capacity: {
      mode: "none",
      capacity: null,
      used: 0,
      fill_ratio: null,
      is_full: false,
      is_overfull: false,
      unit: "count",
    },
    lots: [],
    ...overrides,
  };
}

function stub(handler: (pathname: string) => unknown): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const body = handler(url.pathname);
      return new Response(JSON.stringify(body ?? { detail: "unstubbed" }), {
        status: body === undefined ? 500 : 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the storage map at a project's staging box", () => {
  function renderTreeAt(id: number): void {
    stub((pathname) => (pathname === "/api/locations/tree" ? { nodes: NODES } : undefined));
    render(
      <MemoryRouter initialEntries={[`/tree?at=${id}`]}>
        <Routes>
          <Route path="/tree" element={<TreeScreen />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("does not tell the reader to empty a box that is doing its job", async () => {
    renderTreeAt(7);

    await waitFor(() => {
      expect(screen.getByText("These parts are set aside for a project")).toBeTruthy();
    });
    // The exact sentence that was wrong, asserted as absent rather than by
    // checking the new one alone: adding a second notice without removing the
    // first would read as a contradiction and would pass a positive-only test.
    expect(screen.queryByText(/meant to be emptied rather than lived in/)).toBeNull();
    expect(screen.queryByText("This is the staging inbox")).toBeNull();
  });

  it("still says exactly that about the real INBOX", async () => {
    renderTreeAt(5);

    await waitFor(() => {
      expect(screen.getByText("This is the staging inbox")).toBeTruthy();
    });
    expect(screen.getByText(/meant to be emptied rather than lived in/)).toBeTruthy();
    expect(screen.queryByText("These parts are set aside for a project")).toBeNull();
  });

  it("gives the two kinds different badge words, not one shared hue", async () => {
    renderTreeAt(6);

    // Both are on screen at once here — the PROJECTS root and the INBOX are
    // siblings — so a badge that read "inbox" on both would be indistinguishable
    // for a reader who cannot rely on colour.
    await waitFor(() => {
      expect(screen.getAllByText("project parts").length).toBeGreaterThan(0);
    });
    expect(screen.getAllByText("inbox")).toHaveLength(1);
  });
});

describe("the bin screen at a project's staging box", () => {
  it("names it as project parts rather than as the inbox", async () => {
    stub((pathname) => (pathname === "/api/locations/7" ? locationRead({}) : undefined));
    render(
      <MemoryRouter initialEntries={["/locations/7"]}>
        <Routes>
          <Route path="/locations/:locationId" element={<LocationScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("project parts")).toBeTruthy();
    });
    expect(screen.queryByText("inbox")).toBeNull();
  });

  it("still calls the INBOX the inbox", async () => {
    stub((pathname) =>
      pathname === "/api/locations/5"
        ? locationRead({
            id: 5,
            name: "INBOX",
            parent_id: null,
            depth: 0,
            id_path: "/5/",
            label_path: "INBOX",
            slot_label: null,
            // Null, not true: the INBOX inherits placeability, which is precisely
            // why the discriminator is `=== false` and not truthiness.
            is_placeable: null,
          })
        : undefined,
    );
    render(
      <MemoryRouter initialEntries={["/locations/5"]}>
        <Routes>
          <Route path="/locations/:locationId" element={<LocationScreen />} />
        </Routes>
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(screen.getByText("inbox")).toBeTruthy();
    });
    expect(screen.queryByText("project parts")).toBeNull();
  });
});
