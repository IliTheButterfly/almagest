/**
 * Part detail, and the ASSIGN gap the bug report was about: "I set the part
 * as done and then couldn't find it. I'm not sure I understand how to select
 * a container for the part."
 *
 * The backend was never the problem — `parts` is a definition and creating one
 * deliberately puts it nowhere. These tests pin the UI fix: a zero-stock part
 * says so in words that teach the three-tier model, and offers the suggested
 * location immediately rather than behind a click nobody knew to make.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PartScreen } from "./PartScreen";

const ZERO_STOCK_PART = {
  id: 9,
  name: "Unnamed capacitor",
  mpn: null,
  description: null,
  is_stub: true,
  is_active: true,
  category_id: null,
  manufacturer_id: null,
  package_type_id: null,
  part_kind: "component",
  keywords: null,
  notes: null,
  mpn_norm: null,
  short_id: null,
  hot_score: 0,
  total_qty_milli: 0,
  unit_mass_mg: null,
  unit_volume_mm3: null,
  uom_id: null,
  volume_source: null,
  length_mm: null,
  width_mm: null,
  height_mm: null,
  shape_factor: null,
  lots: [],
};

const STOCKED_PART = {
  ...ZERO_STOCK_PART,
  id: 10,
  name: "22uF 25V ceramic",
  is_stub: false,
  total_qty_milli: 5_000,
  lots: [
    {
      id: 55,
      part_id: 10,
      location_id: 3,
      qty_milli: 5_000,
      qty_reserved_milli: 0,
      status: "active",
      location_label_path: "Cabinet A / Drawer A1",
      batch_code: null,
    },
  ],
};

const SUGGESTION = {
  location_id: 42,
  label_path: "Cabinet B / Drawer B3",
  escalation_level: "preferred",
  reason: "matches the part's package family",
  candidates: [
    { location_id: 42, label_path: "Cabinet B / Drawer B3", score: 0.9, free_capacity: 10 },
  ],
  replayed: false,
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(part: Record<string, unknown>): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });

      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === `/api/parts/${String(part["id"])}` && request.method === "GET") {
        return json(part);
      }
      if (
        url.pathname === `/api/parts/${String(part["id"])}/documents` &&
        request.method === "GET"
      ) {
        // No datasheet section under test here — `DocumentsPanel.test.tsx`
        // covers the panel itself. This just keeps `PartScreen`'s own mount
        // from throwing on an unstubbed request.
        return json({ part_id: part["id"], links: [] });
      }
      if (url.pathname === "/api/locations/suggest") {
        return json(SUGGESTION);
      }
      if (url.pathname === "/api/stock/receive") {
        return json({
          seqs: [1],
          lot: {
            id: 900,
            part_id: part["id"],
            location_id: SUGGESTION.location_id,
            qty_milli: 3_000,
            qty_reserved_milli: 0,
            status: "active",
            location_label_path: SUGGESTION.label_path,
            batch_code: null,
          },
          replayed: false,
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function callTo(pathname: string): Call | undefined {
  return calls.find((call) => call.url === pathname);
}

function renderPart(partId: number) {
  return render(
    <MemoryRouter initialEntries={[`/parts/${partId}`]}>
      <Routes>
        <Route path="/parts/:partId" element={<PartScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a part with no stock", () => {
  it("says, in one sentence, that a part is a definition and stock lives in lots at locations", async () => {
    stubApi(ZERO_STOCK_PART);
    renderPart(9);

    // This exact teaching sentence is the fix — pinned so a refactor that
    // drops it back to a bare "None" fails loudly.
    expect(
      await screen.findByText(
        /a part is a definition, not a quantity\. Stock lives in\s*lots at locations/,
      ),
    ).toBeTruthy();
  });

  it("fetches a suggested location on its own, with no button to find first", async () => {
    stubApi(ZERO_STOCK_PART);
    renderPart(9);

    // No click anywhere — the suggestion is fetched because there is no
    // stock yet, not because someone found a "Suggest" button.
    await waitFor(() => expect(callTo("/api/locations/suggest")).toBeDefined());
    expect(await screen.findByText("Cabinet B / Drawer B3")).toBeTruthy();
    expect(screen.getByText(/This part has no stock yet/)).toBeTruthy();
  });

  it("commits the suggested location and quantity through /api/stock/receive", async () => {
    stubApi(ZERO_STOCK_PART);
    renderPart(9);

    await screen.findByText("Cabinet B / Drawer B3");
    // Default quantity is one whole unit (1000 milli); commit as-is.
    fireEvent.click(await screen.findByRole("button", { name: /^Put 1 in Cabinet B/ }));

    await waitFor(() => expect(callTo("/api/stock/receive")).toBeDefined());
    const body = callTo("/api/stock/receive")?.body;
    expect(body?.["part_id"]).toBe(9);
    expect(body?.["location_id"]).toBe(42);
    expect(body?.["qty_milli"]).toBe(1000);
  });

  it("lets an override location id take priority over the suggestion", async () => {
    stubApi(ZERO_STOCK_PART);
    renderPart(9);

    await screen.findByText("Cabinet B / Drawer B3");
    fireEvent.click(screen.getByText("Use a different location instead"));
    fireEvent.change(screen.getByPlaceholderText("42"), { target: { value: "7" } });

    fireEvent.click(await screen.findByRole("button", { name: /^Put 1 in location 7$/ }));

    await waitFor(() => expect(callTo("/api/stock/receive")).toBeDefined());
    expect(callTo("/api/stock/receive")?.body["location_id"]).toBe(7);
  });
});

describe("a part that already has stock", () => {
  it("lists the lot rather than the zero-stock explanation", async () => {
    stubApi(STOCKED_PART);
    renderPart(10);

    expect(await screen.findByText("Cabinet A / Drawer A1")).toBeTruthy();
    expect(screen.queryByText(/a part is a definition, not a quantity/)).toBeNull();
  });

  it("does not fetch a suggestion until asked — picking up a second lot is not urgent", async () => {
    stubApi(STOCKED_PART);
    renderPart(10);

    await screen.findByText("Cabinet A / Drawer A1");
    expect(callTo("/api/locations/suggest")).toBeUndefined();
    expect(screen.getByRole("button", { name: "Suggest a location" })).toBeTruthy();
  });
});
