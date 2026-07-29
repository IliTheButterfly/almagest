/**
 * The BOM table, exercised against a stubbed `fetch`.
 *
 * The behaviours under test are the ones the design problem is actually about:
 * an unmatched line looks nothing like a matched one and is one tap from being
 * fixed; an auto-matched-but-unconfirmed line is a third, distinct state and not
 * folded into either; DNP is reported, not hidden; and an import says honestly
 * how many lines needed a human.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

function bomLine(overrides: Partial<Record<string, unknown>>) {
  return {
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
    ...overrides,
  };
}

const UNMATCHED = bomLine({ id: 1, line_no: 1, designators: "R1" });
const AUTO_MATCHED = bomLine({
  id: 2,
  line_no: 2,
  designators: "R2",
  part_id: 42,
  is_match_confirmed: false,
});
const MATCHED = bomLine({
  id: 3,
  line_no: 3,
  designators: "R3",
  part_id: 43,
  is_match_confirmed: true,
});
const DNP = bomLine({ id: 4, line_no: 4, designators: "R4", is_dnp: true });

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(
  options: {
    lines?: readonly unknown[];
    searchResults?: readonly unknown[];
    importResponse?: Record<string, unknown>;
  } = {},
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname + url.search,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });

      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/projects/1") {
        return json(PROJECT);
      }
      if (url.pathname === "/api/projects/1/bom" && request.method === "GET") {
        const lines = options.lines ?? [UNMATCHED, AUTO_MATCHED, MATCHED, DNP];
        return json({ total: lines.length, lines });
      }
      if (url.pathname === "/api/projects/1/bom" && request.method === "PUT") {
        const body = JSON.parse(raw) as { edits: { id: number }[] };
        const source = options.lines ?? [UNMATCHED, AUTO_MATCHED, MATCHED, DNP];
        return json({ lines: source.filter((line) => body.edits.some((e) => e.id === (line as { id: number }).id)) });
      }
      if (url.pathname === "/api/projects/1/bom/import") {
        return json(
          options.importResponse ?? {
            project_id: 1,
            lines: [UNMATCHED],
            matched_count: 0,
            unmatched_count: 1,
            dnp_count: 0,
            ambiguous_keys: [],
            warnings: [],
            replayed: false,
          },
        );
      }
      if (url.pathname === "/api/search/parts") {
        return json({
          results: options.searchResults ?? [
            { id: 99, name: "10k 0603 resistor", mpn: "RC0603FR-0710KL", description: null, is_stub: false, category_id: null, lot_count: 0, location_count: 0, qty_milli: 0 },
          ],
          total: 1,
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}${url.search}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/projects/1/bom"]}>
      <Routes>
        <Route path="/projects/:projectId/bom" element={<BomScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

function callsTo(pathname: string): Call[] {
  return calls.filter((call) => call.url === pathname);
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the three BOM line states", () => {
  it("marks an unmatched line distinctly and offers a one-tap match", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText("unmatched")).toBeTruthy();
    // The row that offers it is the unmatched one specifically, not any row.
    const matchButtons = screen.getAllByRole("button", { name: "Match" });
    expect(matchButtons).toHaveLength(1);
  });

  it("marks an auto-matched, unconfirmed line as its own state, not 'matched'", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText("auto-matched")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Confirm match" })).toBeTruthy();
    // Only one row is unqualified 'matched' — the auto-matched one is not it.
    expect(screen.getAllByText("matched")).toHaveLength(1);
  });

  it("shows a human-confirmed match as 'matched' with no match action needed", async () => {
    stubApi();
    renderScreen();

    await screen.findByText("matched");
    // Exactly one line is unconfirmed (AUTO_MATCHED) and offers "Confirm
    // match" — the confirmed line (MATCHED) must not be a second one.
    expect(screen.getAllByRole("button", { name: "Confirm match" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Change match" })).toHaveLength(1);
  });

  it("reports a DNP line rather than hiding it", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText("DNP")).toBeTruthy();
  });

  it("confirming an auto-matched line sends is_match_confirmed:true for that line only", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Confirm match" }));

    await waitFor(() => expect(callsTo("/api/projects/1/bom").length).toBeGreaterThan(0));
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    expect(put?.body["edits"]).toEqual([{ id: AUTO_MATCHED.id, is_match_confirmed: true }]);
  });

  it("matching an unmatched line searches and applies the picked part_id", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Match" }));
    fireEvent.click(await screen.findByRole("button", { name: "Search" }));

    fireEvent.click(await screen.findByRole("button", { name: "Use this" }));

    await waitFor(() => {
      const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(put).toBeDefined();
    });
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    expect(put?.body["edits"]).toEqual([{ id: UNMATCHED.id, part_id: 99 }]);
  });
});

describe("filtering to unmatched only", () => {
  it("asks the server for unmatched lines only when toggled", async () => {
    stubApi();
    renderScreen();

    await screen.findByText("unmatched");
    fireEvent.click(screen.getByRole("button", { name: "Unmatched only" }));

    await waitFor(() =>
      expect(callsTo("/api/projects/1/bom?unmatched_only=true&limit=1000").length).toBeGreaterThan(0),
    );
  });
});

describe("importing a CSV", () => {
  it("reports honestly how many lines landed, matched, and still need a human", async () => {
    stubApi({
      importResponse: {
        project_id: 1,
        lines: [UNMATCHED, AUTO_MATCHED, MATCHED],
        matched_count: 1,
        unmatched_count: 1,
        dnp_count: 1,
        ambiguous_keys: ["10K0603"],
        warnings: ["quantity '4,700' contains a comma; ambiguous decimal, ignored"],
        replayed: false,
      },
    });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Import CSV" }));
    fireEvent.change(screen.getByPlaceholderText(/Reference,Value/), {
      target: { value: "Reference,Value,Footprint,Qty\nR1,10k,0603,1\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    expect(
      await screen.findByText(/3 line\(s\) landed — 1 matched by exact part number, 1 marked DNP/),
    ).toBeTruthy();
    expect(screen.getByText(/1 need a human to say what the part is/)).toBeTruthy();
    expect(screen.getByText(/10K0603/)).toBeTruthy();

    const post = calls.find((call) => call.url === "/api/projects/1/bom/import");
    expect(post?.body["content"]).toContain("R1,10k,0603,1");
    expect(post?.body["match"]).toBe(true);
    expect(typeof post?.body["client_op_id"]).toBe("string");
  });

  it("refuses to submit an empty import", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Import CSV" }));
    expect(screen.getByRole("button", { name: "Import" })).toHaveProperty("disabled", true);
    expect(callsTo("/api/projects/1/bom/import")).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Adding, editing and removing a line by hand
// ---------------------------------------------------------------------------

/**
 * A stateful stand-in for the server: `PUT` actually mutates the list `GET`
 * serves next, so a test can assert on what the *screen* shows after a
 * round trip — the row appearing, being edited, or disappearing — rather
 * than only on the request body sent.
 */
function stubMutableApi(initial: readonly Record<string, unknown>[]): void {
  let lines = [...initial];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname + url.search,
        method: request.method,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });
      const json = (body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/projects/1") {
        return json(PROJECT);
      }
      if (url.pathname === "/api/projects/1/bom" && request.method === "GET") {
        return json({ total: lines.length, lines });
      }
      if (url.pathname === "/api/projects/1/bom" && request.method === "PUT") {
        const body = JSON.parse(raw) as { edits: Record<string, unknown>[] };
        const touched: Record<string, unknown>[] = [];
        const deletedIds: number[] = [];
        let nextLineNo = lines.reduce((max, l) => Math.max(max, l["line_no"] as number), 0);
        for (const edit of body.edits) {
          if (edit["id"] === undefined || edit["id"] === null) {
            nextLineNo += 1;
            const created = bomLine({
              id: 1000 + nextLineNo,
              line_no: nextLineNo,
              designators: null,
              ref_value: null,
              mpn_raw: null,
              manufacturer_raw: null,
              part_id: null,
              is_match_confirmed: false,
              ...edit,
            });
            lines = [...lines, created];
            touched.push(created);
            continue;
          }
          if (edit["delete"] === true) {
            deletedIds.push(edit["id"] as number);
            lines = lines.filter((l) => l["id"] !== edit["id"]);
            continue;
          }
          lines = lines.map((l) => (l["id"] === edit["id"] ? { ...l, ...edit } : l));
          const updated = lines.find((l) => l["id"] === edit["id"]);
          if (updated !== undefined) {
            touched.push(updated);
          }
        }
        return json({ lines: touched, deleted_ids: deletedIds });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}${url.search}`);
    }),
  );
}

describe("adding a line by hand", () => {
  it("sends a create edit with no id and the typed fields", async () => {
    stubMutableApi([UNMATCHED]);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Add a line" }));
    fireEvent.change(screen.getByPlaceholderText("R7, or R7,R8"), {
      target: { value: "C9" },
    });
    fireEvent.change(screen.getByLabelText("Quantity per assembly"), {
      target: { value: "2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add line" }));

    await waitFor(() => {
      const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(put).toBeDefined();
    });
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    expect(put?.body["edits"]).toEqual([
      { qty_per_assembly_milli: 2000, designators: "C9", part_id: null },
    ]);

    // The added line is unmatched — a normal, expected state — and shows up
    // on the next read without a page reload.
    expect(await screen.findByText("C9")).toBeTruthy();
  });

  it("requires a quantity greater than zero before the button is enabled", async () => {
    stubMutableApi([UNMATCHED]);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Add a line" }));
    fireEvent.change(screen.getByLabelText("Quantity per assembly"), {
      target: { value: "0" },
    });
    expect(screen.getByRole("button", { name: "Add line" })).toHaveProperty("disabled", true);
  });
});

describe("editing a line by hand", () => {
  it("sends the designators, value and quantity for that line's id", async () => {
    stubMutableApi([UNMATCHED]);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    // Only one line is stubbed, so its designator input ("R1") is the only
    // match — the row's own title text is not a form control and cannot be
    // confused with it.
    fireEvent.change(screen.getByDisplayValue("R1"), { target: { value: "R1,R9" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(put).toBeDefined();
    });
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    expect(put?.body["edits"]).toEqual([
      { id: UNMATCHED.id, designators: "R1,R9", ref_value: "10k", qty_per_assembly_milli: 1000 },
    ]);
  });
});

describe("removing a line by hand", () => {
  it("sends delete:true for that line's id and the row disappears from the list", async () => {
    stubMutableApi([UNMATCHED, MATCHED]);
    renderScreen();

    const designator = await screen.findByText("R1");
    // Scoped to R1's own row: two lines are stubbed, so a bare `getByRole`
    // would be ambiguous between the two "Remove line" buttons, and picking
    // "whichever is first" would silently stop proving *this* row was the
    // one removed the moment the render order changed.
    const row = designator.closest("li");
    if (row === null) {
      throw new Error("R1's row was not found");
    }
    fireEvent.click(within(row).getByRole("button", { name: "Remove line" }));

    await waitFor(() => {
      const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(put).toBeDefined();
    });
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    expect(put?.body["edits"]).toEqual([{ id: UNMATCHED.id, delete: true }]);

    await waitFor(() => expect(screen.queryByText("R1")).toBeNull());
    // The other line is untouched — a delete removes one row, not the table.
    expect(screen.getByText("R3")).toBeTruthy();
  });

  it("does not release stock already allocated against the deleted line", async () => {
    // The server is the source of truth for that guarantee (an allocation's
    // `bom_line_id` is set NULL, not released) — this only pins that the UI
    // issues a plain delete, with no separate "release holds" request beside
    // it that could be mistaken for doing that itself.
    stubMutableApi([UNMATCHED]);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Remove line" }));

    await waitFor(() => {
      const puts = calls.filter((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(puts).toHaveLength(1);
    });
    expect(calls.filter((call) => call.url.includes("/release"))).toHaveLength(0);
  });
});
