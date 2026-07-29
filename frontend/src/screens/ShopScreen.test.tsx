/**
 * Filling the cart from the real search screen.
 *
 * The point ADR 0007 insists on is asserted here rather than assumed: this view is
 * the *whole* faceted search — the category rail, the filter counts and the stock
 * on every row — with an add button, not a cut-down picker with a text box. If a
 * future change forks it, the first two assertions fail.
 *
 * The badge count is exercised through `App`, because "visible from every screen
 * that can add to it" is a claim about the shell, not about the cart screen.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { shoppingCart } from "../lib/cart/cart";
import { ShopScreen } from "./ShopScreen";

const CATEGORIES = [
  { slug: "passive", name: "Passives", parent_slug: null, depth: 0, part_count: 12 },
  { slug: "capacitor", name: "Capacitors", parent_slug: "passive", depth: 1, part_count: 7 },
];

const TEMPLATES = [
  {
    name: "mounting_type",
    display_name: "Mounting",
    value_type: "enum",
    base_unit: null,
    substitution_direction: "exact",
    sort_order: 10,
    populated_count: 7,
    choices: [
      { key: "THT", label: "Through-hole", count: 4 },
      { key: "SMD", label: "Surface mount", count: 3 },
    ],
  },
];

function part(id: number, overrides: Record<string, unknown> = {}) {
  return {
    id,
    name: `part ${id}`,
    mpn: `MPN-${id}`,
    description: null,
    is_stub: false,
    category_id: 3,
    lot_count: 1,
    location_count: 1,
    qty_milli: 5_000,
    ...overrides,
  };
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      if (url.pathname === "/api/part-categories") {
        return json(CATEGORIES);
      }
      if (url.pathname === "/api/parameter-templates") {
        return json({ total: 1, templates: TEMPLATES });
      }
      if (url.pathname === "/api/search/parts") {
        return json({ total: 2, results: [part(1), part(2)] });
      }
      if (url.pathname === "/api/intake/pending") {
        return json({ total: 0, entries: [] });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderShop(): void {
  render(
    <MemoryRouter initialEntries={["/cart/add"]}>
      <Routes>
        <Route path="/cart/add" element={<ShopScreen />} />
        <Route path="/cart" element={<p>the cart screen</p>} />
        <Route path="/parts/:partId" element={<p>a part screen</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  shoppingCart.clear();
  // The wide layout, so the rail and the facets are the permanent sidebar rather
  // than a collapsed <details>.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: true,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
  stubApi();
});

afterEach(() => {
  shoppingCart.clear();
  vi.unstubAllGlobals();
});

describe("the shopping view", () => {
  it("is the search screen itself — rail, facet counts and stock per row", async () => {
    renderShop();

    // The category rail and a facet count: what a one-field picker cannot express,
    // and the reason this is a prop on `SearchScreen` rather than a second screen.
    expect(await screen.findByText("Capacitors")).toBeTruthy();
    expect(await screen.findByText(/Through-hole/)).toBeTruthy();
    expect((await screen.findAllByText(/5 in stock/)).length).toBe(2);
  });

  it("adds a part to the cart without writing anything", async () => {
    renderShop();
    const add = await screen.findAllByRole("button", { name: "Add to cart" });
    const before = vi.mocked(globalThis.fetch).mock.calls.length;

    fireEvent.click(add[0] as HTMLElement);

    expect(shoppingCart.size).toBe(1);
    expect(shoppingCart.lines()[0]?.partId).toBe(1);
    expect(shoppingCart.lines()[0]?.qtyMilli).toBe(1000);
    // Adding is not a request — the whole reason a cart is safe to browse with.
    // Counted rather than filtered by method: the facet read is itself a POST, so
    // "no writes" has to mean "no traffic at all beyond what was already loaded".
    expect(vi.mocked(globalThis.fetch).mock.calls.length).toBe(before);
  });

  it("presses twice for two of the same part, and says so on the row", async () => {
    renderShop();
    const add = await screen.findAllByRole("button", { name: "Add to cart" });

    fireEvent.click(add[0] as HTMLElement);
    fireEvent.click(add[0] as HTMLElement);

    // One row, quantity two — merged on part-and-lot, per the cart's rule.
    expect(shoppingCart.size).toBe(1);
    expect(shoppingCart.lines()[0]?.qtyMilli).toBe(2000);
    expect(await screen.findByText("2 in the cart")).toBeTruthy();
  });

  it("keeps the row's own link to the part, so browsing is not one-way", async () => {
    renderShop();

    fireEvent.click(await screen.findByText("part 1"));

    expect(await screen.findByText("a part screen")).toBeTruthy();
  });
});

describe("the cart count in the nav", () => {
  it("appears beside every screen once the cart holds something", async () => {
    render(
      <MemoryRouter initialEntries={["/cart/add"]}>
        <App />
      </MemoryRouter>,
    );

    // Empty: a bare label, no parenthetical zero to read as a count of nothing.
    expect(await screen.findByRole("link", { name: "Cart" })).toBeTruthy();

    fireEvent.click((await screen.findAllByRole("button", { name: "Add to cart" }))[0] as HTMLElement);

    await waitFor(() => expect(screen.getByRole("link", { name: "Cart (1)" })).toBeTruthy());
  });
});
