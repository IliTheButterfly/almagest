/**
 * Creating a stub part from the scan screen, and the ASSIGN step right after
 * it — the fix for the bug report: "I set the part as done and then couldn't
 * find it. I'm not sure I understand how to select a container for the
 * part." The old flow ended at "Open part N", which is a dead end because a
 * freshly created part has no stock anywhere. It must not stop there.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { scanSession } from "../lib/scan/session";
import { ScanScreen } from "./ScanScreen";

const UNKNOWN = {
  decoded_kind: "unknown",
  latency_ms: 8,
  normalized: "GARBAGE-CODE",
  scan_event_id: 2,
  status: "unknown",
  suggest_bind: true,
};

const CREATED_PART = {
  id: 77,
  name: "GARBAGE-CODE",
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

const SUGGESTION = {
  location_id: 42,
  label_path: "Cabinet B / Drawer B3",
  escalation_level: "preferred",
  reason: "matches the part's package family",
  candidates: [],
  replayed: false,
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(): void {
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

      if (url.pathname === "/api/scan/resolve") {
        return json(UNKNOWN);
      }
      if (url.pathname === "/api/parts") {
        return json({ part: CREATED_PART, replayed: false });
      }
      if (url.pathname === "/api/scan/alias") {
        return json({
          alias_id: 1,
          alias_kind: "whole_payload",
          code_norm: "GARBAGE-CODE",
          created: true,
          hit_count: 1,
          scan_event_id: 2,
        });
      }
      if (url.pathname === "/api/locations/suggest") {
        return json(SUGGESTION);
      }
      if (url.pathname === "/api/stock/receive") {
        return json({
          seqs: [1],
          lot: {
            id: 900,
            part_id: CREATED_PART.id,
            location_id: SUGGESTION.location_id,
            qty_milli: 1000,
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

function renderScan(): void {
  render(
    <MemoryRouter initialEntries={["/scan"]}>
      <ScanScreen />
    </MemoryRouter>,
  );
}

async function scanUnknownCode(): Promise<void> {
  fireEvent.change(screen.getByPlaceholderText(/4K7T-92M8/), {
    target: { value: "GARBAGE-CODE" },
  });
  fireEvent.click(screen.getByRole("button", { name: /look up/i }));
  await screen.findByText("Nothing matched — teach it");
}

beforeEach(() => {
  calls.length = 0;
  scanSession.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("creating a stub part", () => {
  it("does not stop at 'created' — the next thing on screen is somewhere to put it", async () => {
    stubApi();
    renderScan();
    await scanUnknownCode();

    fireEvent.click(screen.getByRole("button", { name: /Create a stub part and bind this code/ }));

    await waitFor(() => expect(callTo("/api/parts")).toBeDefined());
    expect(await screen.findByText(/Created — it has no stock yet/)).toBeTruthy();

    // The suggestion fetches itself; nothing to click to discover it exists.
    await waitFor(() => expect(callTo("/api/locations/suggest")).toBeDefined());
    expect(await screen.findByText("Cabinet B / Drawer B3")).toBeTruthy();
    expect(
      screen.getByText(/does not exist anywhere until some\s*quantity of it is put in a lot at a location/),
    ).toBeTruthy();
  });

  it("commits the suggested location through /api/stock/receive and confirms it landed", async () => {
    stubApi();
    renderScan();
    await scanUnknownCode();
    fireEvent.click(screen.getByRole("button", { name: /Create a stub part and bind this code/ }));

    await screen.findByText("Cabinet B / Drawer B3");
    fireEvent.click(await screen.findByRole("button", { name: /^Put 1 in Cabinet B/ }));

    await waitFor(() => expect(callTo("/api/stock/receive")).toBeDefined());
    const body = callTo("/api/stock/receive")?.body;
    expect(body?.["part_id"]).toBe(77);
    expect(body?.["location_id"]).toBe(42);
    expect(await screen.findByText("On the shelf")).toBeTruthy();
  });

  it("only returns to scanning when told to, not automatically on create", async () => {
    stubApi();
    renderScan();
    await scanUnknownCode();
    fireEvent.click(screen.getByRole("button", { name: /Create a stub part and bind this code/ }));

    await screen.findByText(/Created — it has no stock yet/);
    fireEvent.click(screen.getByRole("button", { name: "Back to scanning" }));

    expect(screen.queryByText(/Created — it has no stock yet/)).toBeNull();
  });
});
