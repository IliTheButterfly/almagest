/**
 * The reported bug, as a test: **Enter in the part picker searches.**
 *
 * "When pressing enter on the pick a part for project BOM, it adds the line
 * instead of searching." The picker's field lives inside `AddBomLine`'s `<form>`,
 * so a bare `<input>` gets the browser's implicit-submit behaviour and the form's
 * `onSubmit` writes a BOM line — with a plausible default quantity, so it
 * succeeds silently and the user is left with a line they did not ask for and no
 * search results.
 *
 * Asserting "no PUT was issued" is **not** what pins that, though it reads like
 * it: jsdom does not implement implicit form submission, so a synthetic `keyDown`
 * never produces a `submit` and that expectation passes whether the handler exists
 * or not. It is kept as a guard against some *other* path writing, and the real
 * assertions are that a search fired and that the keypress came back **cancelled**
 * — `preventDefault` being what suppresses a real browser's implicit submit.
 *
 * `stopPropagation` cannot be observed here at all: React delegates listeners at
 * the root container, so the native event reaches the enclosing form either way
 * and only a real browser's submit would tell them apart. It is belt-and-braces in
 * the handler, and this file does not pretend to cover it.
 *
 * The deliberate "Add line" click is asserted separately, because a fix that broke
 * it would only move the complaint.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BomScreen } from "./BomScreen";

const PROJECT = {
  id: 1,
  name: "Widget rev B",
  revision: "B",
  status: "active",
  description: null,
  source_ref: null,
  notes: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  builds: [],
};

const LINE = {
  id: 1,
  project_id: 1,
  line_no: 1,
  designators: "R1",
  qty_per_assembly_milli: 1000,
  part_id: null,
  is_match_confirmed: false,
  is_dnp: false,
  ref_value: "10k",
  footprint: "0603",
  mpn_raw: "RC0603FR-0710KL",
  mpn_norm: "RC0603FR0710KL",
  manufacturer_raw: "Yageo",
  description: null,
  note: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const RESULT = {
  id: 99,
  name: "10k 0603 resistor",
  mpn: "RC0603FR-0710KL",
  description: null,
  is_stub: false,
  category_id: null,
  lot_count: 1,
  location_count: 1,
  qty_milli: 4_000,
};

interface Call {
  readonly path: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

let calls: Call[] = [];

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        path: url.pathname,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });
      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/projects/1" && request.method === "GET") {
        return json(PROJECT);
      }
      if (url.pathname === "/api/projects/1/bom" && request.method === "GET") {
        return json({ total: 1, lines: [LINE] });
      }
      if (url.pathname === "/api/projects/1/bom" && request.method === "PUT") {
        return json({ lines: [LINE], deleted_ids: [] });
      }
      if (url.pathname === "/api/search/parts") {
        return json({ total: 1, results: [RESULT] });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen(): void {
  render(
    <MemoryRouter initialEntries={["/projects/1/bom"]}>
      <Routes>
        <Route path="/projects/:projectId/bom" element={<BomScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

const writes = (): Call[] => calls.filter((call) => call.method === "PUT");
const searches = (): Call[] => calls.filter((call) => call.path === "/api/search/parts");

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Enter in the part picker", () => {
  /** Opens the add-line form and its picker — the exact path in the report. */
  async function openPickerInTheAddForm(): Promise<HTMLElement> {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Add a line" }));
    fireEvent.click(await screen.findByRole("button", { name: "Pick a part" }));
    return await screen.findByLabelText("Search parts to match");
  }

  it("searches instead of adding the line", async () => {
    const field = await openPickerInTheAddForm();
    // The picker really is inside the form whose submit handler wrote the line —
    // the arrangement the bug depended on, so if it is ever restructured this test
    // should be re-read rather than trusted.
    expect(field.closest("form")).not.toBeNull();

    fireEvent.change(field, { target: { value: "10k 0603" } });
    // `fireEvent` returns false when the event was cancelled — which is what
    // `preventDefault` does, and what suppresses a real browser's implicit submit.
    const notCancelled = fireEvent.keyDown(field, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(searches()).toHaveLength(1));
    expect(searches()[0]?.body["text"]).toBe("10k 0603");
    expect(notCancelled).toBe(false);
    expect(writes()).toHaveLength(0);
    // …and the results are on screen, which is what the keypress was for.
    expect(await screen.findByText("10k 0603 resistor")).toBeTruthy();
  });

  it("still adds the line when the Add button itself is pressed", async () => {
    // The guard must be a keypress fix, not a broken form: the deliberate
    // submission has to keep working, or "Enter does nothing" is the next report.
    await openPickerInTheAddForm();

    fireEvent.click(screen.getByRole("button", { name: "Add line" }));

    await waitFor(() => expect(writes()).toHaveLength(1));
  });

  it("shows how much of a candidate is in stock, as the search list does", async () => {
    // The picker used to omit this, so the same part read as two different
    // records depending on which screen you met it on — and stock on hand is the
    // one number that matters when choosing between candidates.
    await openPickerInTheAddForm();

    fireEvent.keyDown(await screen.findByLabelText("Search parts to match"), { key: "Enter" });

    expect(await screen.findByText(/4 in stock/)).toBeTruthy();
  });
});
