/**
 * Take/return, exercised end to end against a stubbed `fetch`.
 *
 * These are the behaviours that make the screen safe rather than merely working:
 * the key sent with the commit is the one the scan minted, a replayed response gets
 * no undo button, and the undo posts the committed key back.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { scanSession } from "../lib/scan/session";
import { LotScreen } from "./LotScreen";

const LOT = {
  id: 7,
  part_id: 3,
  location_id: 11,
  qty_milli: 1_200_000,
  qty_reserved_milli: 0,
  status: "active",
  location_label_path: "Cabinet A / Drawer A1",
  batch_code: null,
};

const PART = {
  id: 3,
  name: "22uF 25V ceramic, through-hole",
  mpn: "DEMO-CAP-THT-22U",
  description: null,
  is_stub: false,
  is_active: true,
  category_id: null,
  manufacturer_id: null,
  package_type_id: null,
  part_kind: "component",
  keywords: null,
  notes: null,
  mpn_norm: "DEMOCAPTHT22U",
  short_id: null,
  hot_score: 0,
  total_qty_milli: 1_200_000,
  unit_mass_mg: null,
  unit_volume_mm3: null,
  uom_id: null,
  volume_source: null,
  length_mm: null,
  width_mm: null,
  height_mm: null,
  shape_factor: null,
  lots: [LOT],
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

/** Routes each request to a canned answer, recording what was sent. */
function stubApi(overrides: { consume?: Record<string, unknown> } = {}): void {
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

      if (url.pathname === "/api/stock/lots/7") {
        return json(LOT);
      }
      if (url.pathname === "/api/parts/3") {
        return json(PART);
      }
      if (url.pathname === "/api/stock/lots/7/consume") {
        return json({
          lot: { ...LOT, qty_milli: 1_198_000 },
          seqs: [42],
          replayed: false,
          ...overrides.consume,
        });
      }
      if (url.pathname === "/api/stock/undo") {
        return json({ lots: [LOT], seqs: [43], reversed_seqs: [42], replayed: false });
      }
      if (url.pathname === "/api/stock/lots/7/history") {
        return json([]);
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/lots/7"]}>
      <Routes>
        <Route path="/lots/:lotId" element={<LotScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

function callTo(pathname: string): Call | undefined {
  return calls.find((call) => call.url === pathname);
}

beforeEach(() => {
  calls.length = 0;
  scanSession.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("take / return", () => {
  it("shows the lot, its derived path and the balance from the cache", async () => {
    stubApi();
    renderScreen();

    // By role, because the part's name now appears twice on purpose: as the
    // heading, and as the last step of the trail above it.
    expect(
      await screen.findByRole("heading", { name: "22uF 25V ceramic, through-hole" }),
    ).toBeTruthy();
    // The derived path is a link to the container, not the dead text it was.
    expect(
      screen.getByRole("link", { name: "Cabinet A / Drawer A1" }).getAttribute("href"),
    ).toBe("/locations/11");
    expect(screen.getByText("1,200")).toBeTruthy();
  });

  it("commits with the idempotency key the scan minted, not a fresh one", async () => {
    stubApi();
    // The key is generated at scan time, before this screen ever renders.
    const session = scanSession.scan("https://almagest.lan/s/4K7T92M8", "QRCode");
    expect(session).not.toBeNull();

    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: /^Take 1$/ }));

    await waitFor(() => expect(callTo("/api/stock/lots/7/consume")).toBeDefined());
    const body = callTo("/api/stock/lots/7/consume")?.body;
    expect(body?.["client_op_id"]).toBe(session?.clientOpId);
    expect(body?.["qty_milli"]).toBe(1000);
  });

  it("offers an eight-second undo when the movement was actually recorded", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: /^Take 1$/ }));

    const undo = await screen.findByRole("button", { name: "Undo" });
    expect(undo).toBeTruthy();
    expect(screen.getByText(/Took 1/)).toBeTruthy();
  });

  it("offers no undo when the server replayed a stored answer", async () => {
    // `replayed: true` means the movement was recorded by an earlier request with
    // the same key. Its undo window closed then, so offering one now would be a lie
    // about what is being reversed.
    stubApi({ consume: { replayed: true } });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: /^Take 1$/ }));

    expect(await screen.findByText("Already recorded")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
    expect(screen.getByText(/undo window for it has closed/)).toBeTruthy();
  });

  it("undoes by naming the committed key, and carries its own key for the undo", async () => {
    stubApi();
    const session = scanSession.scan("REEL", "DataMatrix");
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: /^Take 1$/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));

    await waitFor(() => expect(callTo("/api/stock/undo")).toBeDefined());
    const body = callTo("/api/stock/undo")?.body;
    expect(body?.["client_op_id_to_undo"]).toBe(session?.clientOpId);
    // Its own key, so a double tap on Undo cannot append two compensating rows.
    expect(body?.["client_op_id"]).toBeTypeOf("string");
    expect(body?.["client_op_id"]).not.toBe(session?.clientOpId);

    expect(await screen.findByText("Undone")).toBeTruthy();
  });

  it("spends the key, so a second take does not replay the first", async () => {
    stubApi();
    const session = scanSession.scan("REEL", "DataMatrix");
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: /^Take 1$/ }));
    await waitFor(() => expect(callTo("/api/stock/lots/7/consume")).toBeDefined());
    expect(scanSession.current()).toBeNull();

    fireEvent.click(await screen.findByRole("button", { name: /^Take 1$/ }));
    await waitFor(() =>
      expect(calls.filter((call) => call.url === "/api/stock/lots/7/consume")).toHaveLength(2),
    );

    const [first, second] = calls.filter((call) => call.url === "/api/stock/lots/7/consume");
    expect(first?.body["client_op_id"]).toBe(session?.clientOpId);
    expect(second?.body["client_op_id"]).not.toBe(first?.body["client_op_id"]);
  });

  it("warns but still commits when the take drives the balance negative", async () => {
    stubApi();
    renderScreen();

    // 2000 pieces out of 1200 on hand.
    fireEvent.click(await screen.findByLabelText("2"));
    fireEvent.click(screen.getByLabelText("0"));
    fireEvent.click(screen.getByLabelText("0"));
    fireEvent.click(screen.getByLabelText("0"));

    expect(screen.getByText(/takes the balance below zero/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /^Take 2,000$/ })).toBeTruthy();
  });

  it("surfaces a refusal without claiming the movement happened", async () => {
    stubApi();
    renderScreen();
    await screen.findByRole("button", { name: /^Take 1$/ });

    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ detail: { reason: "non_positive_quantity", message: "no" } }),
            { status: 409, headers: { "content-type": "application/json" } },
          ),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Take 1$/ }));

    expect(await screen.findByText(/Enter a quantity above zero/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Undo" })).toBeNull();
  });
});
