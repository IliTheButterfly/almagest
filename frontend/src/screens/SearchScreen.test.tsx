/**
 * Faceted search, exercised against a stubbed `fetch`.
 *
 * These are the behaviours that make the panel *useful* rather than merely
 * present:
 *
 * - landing with no query lists everything, because browsing must not require
 *   typing;
 * - facets are re-requested when the filters change, since their counts are
 *   computed against the filters already applied — stale counts describe a
 *   different query;
 * - and **not** re-requested when only the page changes, because that is a
 *   wasted round trip for a byte-identical answer;
 * - a zero count is rendered and disabled, never hidden;
 * - typing fires one request per pause, not one per keystroke;
 * - a 422 lands on the row that caused it, saying what was wrong with the value.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SearchScreen } from "./SearchScreen";

const CATEGORIES = [
  { slug: "passive", name: "Passives", parent_slug: null, depth: 0, part_count: 12 },
  { slug: "capacitor", name: "Capacitors", parent_slug: "passive", depth: 1, part_count: 7 },
  // Nothing in stock and nothing catalogued: the case that must still be shown.
  { slug: "inductor", name: "Inductors", parent_slug: "passive", depth: 1, part_count: 0 },
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
      { key: "PANEL", label: "Panel mount", count: 0 },
    ],
  },
  {
    name: "capacitance",
    display_name: "Capacitance",
    value_type: "numeric",
    base_unit: "F",
    substitution_direction: "range_overlap",
    sort_order: 20,
    populated_count: 7,
    choices: [],
    numeric_range: { min: 1e-9, max: 1e-4, unit_symbol: "F" },
  },
];

function part(id: number) {
  return {
    id,
    name: `part ${id}`,
    mpn: `MPN-${id}`,
    description: null,
    is_stub: false,
    category_id: 3,
  };
}

interface Call {
  readonly path: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

let calls: Call[] = [];
/** Set to a `reason` code to make any capacitance filter fail like the server. */
let refuseCapacitance = false;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(total = 120): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ path: url.pathname, method: request.method, body });

      const filters = (body["filters"] ?? []) as { template?: string; value?: string }[];
      const capacitance = filters.find((filter) => filter.template === "capacitance");
      if (refuseCapacitance && capacitance !== undefined) {
        return json(
          {
            detail: {
              template: "capacitance",
              reason: "implausible",
              message: `1e+06 is outside the plausible range for capacitance`,
            },
          },
          422,
        );
      }

      if (url.pathname === "/api/part-categories") {
        return json(CATEGORIES);
      }
      if (url.pathname === "/api/parameter-templates") {
        return json({ total: 7, templates: TEMPLATES });
      }
      if (url.pathname === "/api/search/parts") {
        const offset = Number(body["offset"] ?? 0);
        return json({
          total,
          results: [part(offset + 1), part(offset + 2)],
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

/** Renders the live querystring, so the URL sync is observable. */
function UrlProbe() {
  return <div data-testid="url">{useLocation().search}</div>;
}

function renderSearch(initial = "/search"): void {
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route
          path="/search"
          element={
            <>
              <SearchScreen />
              <UrlProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

/** No jest-dom here, so `disabled` is read off the element itself. */
const disabled = (element: HTMLElement): boolean =>
  (element as HTMLButtonElement | HTMLInputElement).disabled;

const callsTo = (path: string): Call[] => calls.filter((call) => call.path === path);
const searches = (): Call[] => callsTo("/api/search/parts");
const facetCalls = (): Call[] => callsTo("/api/parameter-templates");
const url = (): string => screen.getByTestId("url").textContent ?? "";

/**
 * Waiting for the debounce.
 *
 * Deliberately not a fixed sleep of just over the delay: nineteen test files run
 * in parallel here, and a sleep tuned to an idle machine is a flake on a loaded
 * one. Positive assertions poll (`waitFor` with a generous timeout); "and nothing
 * more happened" is the only place a fixed pause is needed, and it is compared
 * against a count that has already settled.
 */
const SETTLED = { timeout: 4000 } as const;
const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

beforeEach(() => {
  calls = [];
  refuseCapacitance = false;
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
  vi.unstubAllGlobals();
});

describe("landing with no query", () => {
  it("asks for everything rather than waiting to be told what to look for", async () => {
    renderSearch();
    await waitFor(() => expect(searches()).toHaveLength(1));

    const body = searches()[0]?.body;
    expect(body?.["filters"]).toEqual([]);
    expect(body?.["text"]).toBeUndefined();
    expect(body?.["category"]).toBeUndefined();
    expect(body?.["offset"]).toBe(0);
    expect(await screen.findByText("part 1")).toBeTruthy();
  });

  it("fetches the facets and the categories alongside the results", async () => {
    renderSearch();
    await waitFor(() => expect(facetCalls()).toHaveLength(1));
    expect(callsTo("/api/part-categories")).toHaveLength(1);
    // Counts against nothing-applied-yet: the whole catalogue.
    expect(facetCalls()[0]?.body["filters"]).toEqual([]);
  });

  it("adopts a shared link instead of starting empty", async () => {
    renderSearch("/search?category=capacitor&f=mounting_type%3ATHT&page=2");
    await waitFor(() => expect(searches()).toHaveLength(1));

    const body = searches()[0]?.body;
    expect(body?.["category"]).toBe("capacitor");
    expect(body?.["filters"]).toEqual([{ template: "mounting_type", value: "THT" }]);
    expect(body?.["offset"]).toBe(50);
  });
});

describe("the facet counts", () => {
  it("shows a zero-count choice, disabled and struck through, rather than hiding it", async () => {
    renderSearch();
    const zero = await screen.findByRole("checkbox", { name: /Panel mount/ });

    // Zero is the most useful number here: it says "don't bother". Hiding it
    // would make the panel appear to change shape as stock moves.
    expect(disabled(zero)).toBe(true);
    expect(disabled(screen.getByRole("checkbox", { name: /Through-hole/ }))).toBe(false);
    expect(screen.getByText("Panel mount")).toBeTruthy();
    // Struck through by `.zero`, so the state survives greyscale.
    expect(zero.closest("label")?.className).toContain("zero");
  });

  it("disables an empty category but leaves the populated ones alone", async () => {
    renderSearch();
    expect(disabled(await screen.findByRole("button", { name: /Inductors/ }))).toBe(true);
    expect(disabled(screen.getByRole("button", { name: /Capacitors/ }))).toBe(false);
  });

  it("re-requests them with the filter that was just applied", async () => {
    renderSearch();
    await waitFor(() => expect(facetCalls()).toHaveLength(1));

    fireEvent.click(await screen.findByRole("checkbox", { name: /Through-hole/ }));

    // The counts have to be recomputed against the narrowed set — that is the
    // entire point of showing them.
    await waitFor(() => expect(facetCalls()).toHaveLength(2));
    expect(facetCalls()[1]?.body["filters"]).toEqual([
      { template: "mounting_type", value: "THT" },
    ]);
    // And the results follow the same filter.
    await waitFor(() => expect(searches()).toHaveLength(2));
    expect(searches()[1]?.body["filters"]).toEqual([{ template: "mounting_type", value: "THT" }]);
  });

  it("re-requests them when the category changes", async () => {
    renderSearch();
    await waitFor(() => expect(facetCalls()).toHaveLength(1));

    fireEvent.click(await screen.findByRole("button", { name: /Capacitors/ }));

    await waitFor(() => expect(facetCalls()).toHaveLength(2));
    expect(facetCalls()[1]?.body["category"]).toBe("capacitor");
  });

  it("does not re-request them just because the page changed", async () => {
    renderSearch();
    await waitFor(() => expect(facetCalls()).toHaveLength(1));

    fireEvent.click(await screen.findByRole("button", { name: /Next/ }));

    await waitFor(() => expect(searches()).toHaveLength(2));
    expect(searches()[1]?.body["offset"]).toBe(50);
    // Same narrowing, same counts. A second request would be a wasted round trip.
    expect(facetCalls()).toHaveLength(1);
  });
});

describe("the URL", () => {
  it("gains the filter, so the search is a link", async () => {
    renderSearch();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Through-hole/ }));

    await waitFor(() => expect(url()).toContain("f=mounting_type%3ATHT"));
  });

  it("carries the category and drops it again when it is cleared", async () => {
    renderSearch();
    const capacitors = await screen.findByRole("button", { name: /Capacitors/ });

    fireEvent.click(capacitors);
    await waitFor(() => expect(url()).toContain("category=capacitor"));

    fireEvent.click(capacitors);
    await waitFor(() => expect(url()).not.toContain("category=capacitor"));
  });

  it("keeps the page out of the querystring on page one", async () => {
    renderSearch();
    fireEvent.click(await screen.findByRole("button", { name: /Next/ }));
    await waitFor(() => expect(url()).toContain("page=2"));

    fireEvent.click(screen.getByRole("button", { name: /Previous/ }));
    await waitFor(() => expect(url()).not.toContain("page="));
  });
});

describe("typing a range", () => {
  it("sends the shorthand straight through as a comparison", async () => {
    renderSearch();
    const min = await screen.findByLabelText("Capacitance minimum");

    fireEvent.change(min, { target: { value: "20u" } });

    await waitFor(() => expect(searches()).toHaveLength(2), SETTLED);
    // Raw text, not a parsed number: the server parses it with the template's
    // quantity as context, which is the only place that can be done correctly.
    expect(searches()[1]?.body["filters"]).toEqual([
      { template: "capacitance", value: ">=20u" },
    ]);
  });

  it("joins two ends into the documented range form", async () => {
    renderSearch();
    fireEvent.change(await screen.findByLabelText("Capacitance minimum"), {
      target: { value: "20" },
    });
    fireEvent.change(screen.getByLabelText("Capacitance maximum"), { target: { value: "30uF" } });

    await waitFor(() => expect(searches()).toHaveLength(2), SETTLED);
    expect(searches()[1]?.body["filters"]).toEqual([
      { template: "capacitance", value: "20-30uF" },
    ]);
  });

  it("fires once for a burst of keystrokes, not once per character", async () => {
    renderSearch();
    await waitFor(() => expect(searches()).toHaveLength(1));
    const min = await screen.findByLabelText("Capacitance minimum");

    for (const value of ["2", "20", "20u", "20uF"]) {
      fireEvent.change(min, { target: { value } });
    }

    // One extra search and one extra facets request in total — a request per
    // keystroke would be four of each, and a dragged slider far worse.
    await waitFor(() => expect(searches()).toHaveLength(2), SETTLED);
    await pause(250);
    expect(searches()).toHaveLength(2);
    expect(facetCalls()).toHaveLength(2);
    expect(searches()[1]?.body["filters"]).toEqual([
      { template: "capacitance", value: ">=20uF" },
    ]);
  });

  it("does not debounce a ticked box", async () => {
    renderSearch();
    await waitFor(() => expect(searches()).toHaveLength(1));

    fireEvent.click(await screen.findByRole("checkbox", { name: /Surface mount/ }));

    // No timer to wait out: a tap that takes 300ms to acknowledge reads as lost.
    await waitFor(() => expect(searches()).toHaveLength(2));
  });
});

describe("a value the server refuses", () => {
  it("says what is wrong with it, in units rather than in validation-speak", async () => {
    refuseCapacitance = true;
    renderSearch();
    fireEvent.change(await screen.findByLabelText("Capacitance minimum"), {
      target: { value: "1M" },
    });

    // `implausible` under capacitance is the case worth having: 1M parses
    // perfectly and means megafarads. Said twice on purpose — once at the top of
    // the results, once against the offending row.
    expect((await screen.findAllByText(/megafarads/, {}, SETTLED)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/not a thing/).length).toBeGreaterThan(0);
  });

  it("marks the facet that caused it, not just the top of the page", async () => {
    refuseCapacitance = true;
    renderSearch();
    fireEvent.change(await screen.findByLabelText("Capacitance minimum"), {
      target: { value: "1M" },
    });

    await waitFor(() => {
      const invalid = document.querySelectorAll('.facet[data-invalid="true"]');
      expect(invalid).toHaveLength(1);
      expect(invalid[0]?.textContent).toContain("Capacitance");
    }, SETTLED);
  });

  it("keeps the last good results on screen instead of blanking them", async () => {
    renderSearch();
    expect(await screen.findByText("part 1")).toBeTruthy();

    refuseCapacitance = true;
    fireEvent.change(screen.getByLabelText("Capacitance minimum"), { target: { value: "1M" } });

    await screen.findAllByText(/megafarads/, {}, SETTLED);
    // A refused *filter* has not invalidated the parts already listed.
    expect(screen.getByText("part 1")).toBeTruthy();
  });
});
