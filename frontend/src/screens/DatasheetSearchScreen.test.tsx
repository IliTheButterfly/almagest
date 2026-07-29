/**
 * Datasheet full-text search, against a stubbed `fetch`.
 *
 * The behaviours that matter, mirroring `SearchScreen.test.tsx`'s posture for
 * part search:
 *
 * - an empty box fires no request and shows no results — there is no useful
 *   "browse everything" over raw document text;
 * - typing debounces to one request per pause;
 * - the querystring round-trips, so a result page is a shareable link;
 * - a snippet's highlighted term renders as a real `<mark>`, not markup pulled
 *   from the response string;
 * - a hit's title is a plain external link to the document URL, not a router
 *   `<Link>` — the destination is a PDF, not a screen in this app.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DatasheetSearchScreen } from "./DatasheetSearchScreen";

interface Call {
  readonly path: string;
  readonly search: string;
}

let calls: Call[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function hit(sha256: string, snippet: { text: string; highlighted: boolean }[]) {
  return {
    sha256,
    kind: "datasheet",
    media_type: "application/pdf",
    byte_size: 40_000,
    page_count: 4,
    original_filename: `${sha256}.pdf`,
    url: `/api/documents/${sha256}`,
    snippet,
  };
}

function stubApi(total = 1, results = [hit("abc123", [{ text: "ordinary text", highlighted: false }])]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      calls.push({ path: url.pathname, search: url.search });

      if (url.pathname === "/api/search/datasheets") {
        return json({ total, results });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function UrlProbe() {
  return <div data-testid="url">{useLocation().search}</div>;
}

function renderScreen(initial = "/datasheets"): void {
  render(
    <MemoryRouter initialEntries={[initial]}>
      <Routes>
        <Route
          path="/datasheets"
          element={
            <>
              <DatasheetSearchScreen />
              <UrlProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const searches = (): Call[] => calls.filter((call) => call.path === "/api/search/datasheets");
const url = (): string => screen.getByTestId("url").textContent ?? "";
/** Comfortably past the 300ms debounce even under a loaded CI runner. */
const SETTLED = { timeout: 2_000 };

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("an empty query", () => {
  it("fires no request and prompts rather than showing anything", async () => {
    stubApi();
    renderScreen();

    // `findBy*` (rather than a synchronous `getBy*`) so `useAsync`'s no-op
    // `Promise.resolve(null)` for the empty query settles before the
    // assertion, instead of leaving a state update to land after the test body
    // returns.
    expect(await screen.findByText(/Type something to search/)).toBeTruthy();
    expect(searches()).toHaveLength(0);
  });
});

describe("typing a query", () => {
  it("debounces to one request per pause, not one per keystroke", async () => {
    stubApi();
    renderScreen();

    const input = screen.getByLabelText("Search datasheet text");
    for (const value of ["t", "th", "the", "ther", "thermal"]) {
      fireEvent.change(input, { target: { value } });
    }

    await waitFor(() => expect(searches()).toHaveLength(1), SETTLED);
    expect(searches()[0]?.search).toContain("q=thermal");
  });

  it("round-trips the query through the URL", async () => {
    stubApi();
    renderScreen();

    fireEvent.change(screen.getByLabelText("Search datasheet text"), {
      target: { value: "ferrite" },
    });

    await waitFor(() => expect(url()).toContain("q=ferrite"), SETTLED);
  });

  it("clearing the box removes it from the URL and stops showing results", async () => {
    stubApi();
    renderScreen();

    fireEvent.change(screen.getByLabelText("Search datasheet text"), {
      target: { value: "ferrite" },
    });
    await waitFor(() => expect(searches()).toHaveLength(1), SETTLED);

    fireEvent.change(screen.getByLabelText("Search datasheet text"), { target: { value: "" } });
    await waitFor(() => expect(url()).not.toContain("q="), SETTLED);
    expect(screen.getByText(/Type something to search/)).toBeTruthy();
  });
});

describe("results", () => {
  it("shows the matched document as a link to its own URL, not a router route", async () => {
    stubApi(1, [hit("deadbeef", [{ text: "no match here", highlighted: false }])]);
    renderScreen("/datasheets?q=ferrite");

    const link = await screen.findByRole("link", { name: "deadbeef.pdf" });
    expect(link.getAttribute("href")).toBe("/api/documents/deadbeef");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("renders a highlighted snippet segment as a real <mark>, not raw markup", async () => {
    stubApi(1, [
      hit("cafef00d", [
        { text: "operating ", highlighted: false },
        { text: "temperature", highlighted: true },
        { text: " range", highlighted: false },
      ]),
    ]);
    renderScreen("/datasheets?q=temperature");

    const mark = await screen.findByText("temperature");
    expect(mark.tagName).toBe("MARK");
  });

  it("says plainly when nothing matched, rather than an empty list", async () => {
    stubApi(0, []);
    renderScreen("/datasheets?q=zzznonexistent");

    expect(await screen.findByText("No stored PDF's extracted text matches that.")).toBeTruthy();
  });

  it("pages without dropping the query", async () => {
    stubApi(50, [hit("page1", [{ text: "hit", highlighted: true }])]);
    renderScreen("/datasheets?q=widget");

    await screen.findByRole("link", { name: "page1.pdf" });
    fireEvent.click(screen.getByRole("button", { name: "Next →" }));

    await waitFor(() => expect(url()).toContain("page=2"), SETTLED);
    await waitFor(
      () => expect(searches().some((call) => call.search.includes("offset=20"))).toBe(true),
      SETTLED,
    );
    // The query itself must survive the page change.
    expect(searches().at(-1)?.search).toContain("q=widget");
  });
});
