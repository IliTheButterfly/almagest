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

function requirementFixture(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    text: "3x 10k 1% 0603 resistor",
    quantity: 3,
    category: "resistor",
    filters: [
      {
        template: "resistance",
        value: "9900-10100",
        source_text: "10k",
        origin: "deterministic",
        confidence: 1,
      },
    ],
    mpn: null,
    mpn_norm: null,
    residue: [],
    rejections: [],
    notes: [],
    confidence: 1,
    provenance: "deterministic",
    is_actionable: true,
    is_complete: true,
    ...overrides,
  };
}

function candidateFixture(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    rank: 1,
    part_id: 501,
    name: "10k 0603 resistor",
    mpn: "RC0603FR-0710KL",
    description: null,
    is_stub: false,
    category_id: null,
    qty_milli: 5000,
    qty_reserved_milli: 0,
    lot_count: 1,
    location_count: 1,
    is_in_stock: true,
    is_substitute: false,
    covers_required: true,
    distance: 0,
    reasons: [],
    ...overrides,
  };
}

function suggestionLine(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    index: 0,
    bom_line_id: null,
    text: "3x 10k 1% 0603 resistor",
    outcome: "stocked",
    message: "You own a part that satisfies this.",
    required_milli: 3000,
    requirement: requirementFixture(),
    in_stock: [],
    not_stocked: [],
    truncated: false,
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
    suggestResponse?: Record<string, unknown>;
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
      if (url.pathname === "/api/requirements/suggest") {
        return json(options.suggestResponse ?? { lines: [] });
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

  it("does not dress a refused file up as a successful empty import", async () => {
    // The reader refuses a binary workbook by name — the one thing the user can
    // act on. But it refuses it *with 200 and zero lines*, because the importer
    // never fails: so `unmatched_count` is 0, and reading only that number gives a
    // green "Import result: 0 line(s) landed" with the reason folded away behind a
    // summary reading "1 parser warning(s)". That is the silent failure the
    // server-side refusal exists to prevent, reintroduced in the UI.
    stubApi({
      importResponse: {
        project_id: 1,
        lines: [],
        matched_count: 0,
        unmatched_count: 0,
        dnp_count: 0,
        ambiguous_keys: [],
        warnings: [
          "this file is a zip-based spreadsheet (.xlsx, .ods and .numbers all look" +
            " like this), not text; nothing was imported. Re-export the BOM as CSV" +
            " or tab-delimited text — KiCad, Altium's Report Manager and" +
            " CircuitMaker all offer it — and import that.",
        ],
        replayed: false,
      },
    });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Import CSV" }));
    fireEvent.change(screen.getByPlaceholderText(/Reference,Value/), {
      target: { value: "PK   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));

    const heading = await screen.findByText("Nothing was imported");
    expect(heading.closest(".notice")?.className).toContain("notice-warn");
    // ...and the reason is open on arrival, because with no lines the warnings
    // are not an aside about the result, they are the result.
    expect(screen.getByText("Why").closest("details")?.hasAttribute("open")).toBe(true);
    expect(screen.queryByText(/parser warning/)).toBeNull();
    expect(screen.getByText(/Re-export the BOM as CSV/)).toBeTruthy();
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

  it("requires a quantity greater than zero before the button is enabled, and says so", async () => {
    stubMutableApi([UNMATCHED]);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Add a line" }));
    fireEvent.change(screen.getByLabelText("Quantity per assembly"), {
      target: { value: "0" },
    });
    expect(screen.getByRole("button", { name: "Add line" })).toHaveProperty("disabled", true);
    // Clearing the quantity must not leave "Add line" merely inert — the
    // silent failure this test is here to prevent a regression of.
    expect(screen.getByText(/Quantity must be greater than zero/)).toBeTruthy();
    expect(callsTo("/api/projects/1/bom").filter((c) => c.method === "PUT")).toHaveLength(0);
  });

  it("does not silently drop a submit attempt while the quantity is invalid", async () => {
    // A defensive check for the case a disabled button does not cover: some
    // browsers still fire the form's submit event on Enter even with the
    // submit button disabled. Whatever the trigger, the guard inside
    // `submit()` itself must speak up rather than return quietly.
    stubMutableApi([UNMATCHED]);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Add a line" }));
    fireEvent.change(screen.getByLabelText("Quantity per assembly"), {
      target: { value: "0" },
    });
    fireEvent.submit(screen.getByRole("button", { name: "Add line" }).closest("form")!);

    expect(await screen.findByText(/records nothing/)).toBeTruthy();
    expect(callsTo("/api/projects/1/bom").filter((c) => c.method === "PUT")).toHaveLength(0);
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

  it("disables Save and says why when an existing quantity is cleared to zero", async () => {
    stubMutableApi([UNMATCHED]);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Quantity per assembly"), {
      target: { value: "0" },
    });

    expect(screen.getByRole("button", { name: "Save" })).toHaveProperty("disabled", true);
    expect(screen.getByText(/Quantity must be greater than zero/)).toBeTruthy();
    expect(callsTo("/api/projects/1/bom").filter((c) => c.method === "PUT")).toHaveLength(0);
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

// ---------------------------------------------------------------------------
// Pasting requirements: prose in, a batched call to /api/requirements/suggest,
// a review row per line.
// ---------------------------------------------------------------------------

async function openPasteBox(lineText: string): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name: "Paste requirements" }));
  fireEvent.change(screen.getByLabelText("Requirements, one per line"), {
    target: { value: lineText },
  });
  fireEvent.click(screen.getByRole("button", { name: "Suggest parts" }));
}

describe("pasting requirements", () => {
  it("turns a pasted block into one row per line, in one batched call", async () => {
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({ index: 0, text: "3x 10k 1% 0603 resistor" }),
          suggestionLine({
            index: 1,
            text: "100nF 50V X7R 0603",
            requirement: requirementFixture({ text: "100nF 50V X7R 0603", category: "capacitor" }),
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("3x 10k 1% 0603 resistor\n100nF 50V X7R 0603");

    expect(await screen.findByText("3x 10k 1% 0603 resistor")).toBeTruthy();
    expect(screen.getByText("100nF 50V X7R 0603")).toBeTruthy();

    // One request for the whole pasted block, not one per line.
    expect(calls.filter((call) => call.url === "/api/requirements/suggest")).toHaveLength(1);
    const post = calls.find((call) => call.url === "/api/requirements/suggest");
    expect(post?.body["lines"]).toEqual([
      { text: "3x 10k 1% 0603 resistor" },
      { text: "100nF 50V X7R 0603" },
    ]);
  });

  it("shows the unparsed residue rather than hiding it", async () => {
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({
            text: "that thing Dave used on the mixer board",
            outcome: "not_actionable",
            message: "Nothing in this line became a predicate.",
            requirement: requirementFixture({
              text: "that thing Dave used on the mixer board",
              quantity: null,
              category: null,
              filters: [],
              residue: ["that", "thing", "dave", "used", "on", "the", "mixer", "board"],
              confidence: 0,
              provenance: "none",
              is_actionable: false,
              is_complete: false,
            }),
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("that thing Dave used on the mixer board");

    expect(await screen.findByText("not understood")).toBeTruthy();
    expect(screen.getByText(/dave/)).toBeTruthy();
  });

  it("shows confidence as a word, not only a colour", async () => {
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({
            text: "a dual op-amp, rail-to-rail, SOIC-8",
            requirement: requirementFixture({
              text: "a dual op-amp, rail-to-rail, SOIC-8",
              confidence: 0.72,
              provenance: "mixed",
            }),
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("a dual op-amp, rail-to-rail, SOIC-8");

    // The word ("mixed"), not just the badge's colour, is what a test — and a
    // colour-blind reader — can see.
    expect(await screen.findByText("72% mixed")).toBeTruthy();
  });

  it("a row with no suggestion is still acceptable, and its text survives in the note", async () => {
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({
            outcome: "no_match",
            message: "Nothing in the catalogue satisfies this.",
            in_stock: [],
            not_stocked: [],
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("3x 10k 1% 0603 resistor");

    fireEvent.click(await screen.findByRole("button", { name: "Accept without a part" }));

    await waitFor(() => {
      const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(put).toBeDefined();
    });
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    // `qty_per_assembly_milli` is not optional: a `BomLineEdit` with no `id` and
    // no quantity is a 422 (`_create_needs_a_quantity_delete_needs_an_id`), which
    // is what this whole panel used to send. `required_milli` is where it comes
    // from — `3x` said three.
    expect(put?.body["edits"]).toEqual([
      { note: "3x 10k 1% 0603 resistor", part_id: null, qty_per_assembly_milli: 3000 },
    ]);
    expect(await screen.findByText("added to BOM")).toBeTruthy();
  });

  it("accepting a ranked candidate writes that candidate's part_id", async () => {
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({
            outcome: "stocked",
            in_stock: [candidateFixture({ part_id: 501, name: "10k 0603 resistor" })],
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("3x 10k 1% 0603 resistor");

    await screen.findByText("10k 0603 resistor");
    fireEvent.click(screen.getByRole("button", { name: "Use this" }));

    await waitFor(() => {
      const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(put).toBeDefined();
    });
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    expect(put?.body["edits"]).toEqual([
      { note: "3x 10k 1% 0603 resistor", part_id: 501, qty_per_assembly_milli: 3000 },
    ]);
  });

  it("flags 'you own none of these' prominently for order and no_match outcomes", async () => {
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({ outcome: "order", not_stocked: [candidateFixture()] }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("3x 10k 1% 0603 resistor");

    expect(await screen.findByText("You own none of these")).toBeTruthy();
  });

  it("adds every remaining row in one batched call, and attaches no part to any of them", async () => {
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({ index: 0, text: "3x 10k 1% 0603 resistor" }),
          suggestionLine({
            index: 1,
            text: "100nF 50V X7R 0603",
            required_milli: null,
            requirement: requirementFixture({ text: "100nF 50V X7R 0603" }),
            in_stock: [candidateFixture({ part_id: 777, name: "100nF X7R 0603" })],
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("3x 10k 1% 0603 resistor\n100nF 50V X7R 0603");

    await screen.findByText("100nF X7R 0603");
    fireEvent.click(screen.getByRole("button", { name: "Add all without parts" }));

    await waitFor(() => {
      const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
      expect(put).toBeDefined();
    });
    const put = calls.find((call) => call.url === "/api/projects/1/bom" && call.method === "PUT");
    // Line 1 has a rank-1 in-stock candidate and it is still `part_id: null`. The
    // bulk button lands the lines; it never decides which part fills one, because
    // `PUT .../bom` would record that as `is_match_confirmed`.
    expect(put?.body["edits"]).toEqual([
      { note: "3x 10k 1% 0603 resistor", part_id: null, qty_per_assembly_milli: 3000 },
      // `required_milli: null` — the line said nothing about how many, so one per
      // assembly is written rather than a quantity nobody gave.
      { note: "100nF 50V X7R 0603", part_id: null, qty_per_assembly_milli: 1000 },
    ]);
  });
});

/**
 * Regressions for the two defects adversarial review found in this panel. Both
 * were green: `tsc --noEmit` passed, and every test above passed, because a
 * stubbed `fetch` cannot enforce a cross-field pydantic validator and because the
 * accept-all fixture happened to have an exact match at rank 1.
 *
 * `backend/tests/integration/test_bom_intake_findings.py` holds the other half —
 * the server-side contract these bodies have to satisfy — since that is the part
 * a stub can never cover.
 */
describe("regressions: what an accept is allowed to claim", () => {
  it("does not attach a substitute nobody looked at, nor a part the user does not own", async () => {
    // The reproduced case exactly: line 0 is `stocked` with a rank-1 flagged
    // `is_substitute`, and line 1 is `order` — `in_stock` empty, so the old
    // `topCandidate` fell through to a `not_stocked` part with no stock at all.
    // One click wrote both as `part_id`, and `PUT .../bom` records an edit naming
    // a part as `is_match_confirmed = true` unless told otherwise. So one click
    // marked a substitute and a part nobody owns as "a human agreed" — the exact
    // distinction `bom_import._match_lines` refuses to make even for an exact MPN
    // equality.
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({
            index: 0,
            text: "100nF 0603 10% X7R",
            outcome: "stocked",
            in_stock: [
              candidateFixture({ rank: 1, part_id: 777, name: "100nF 1206", is_substitute: true }),
              candidateFixture({ rank: 2, part_id: 888, name: "100nF 0603" }),
            ],
          }),
          suggestionLine({
            index: 1,
            text: "470uF 25V ceramic capacitor",
            outcome: "order",
            required_milli: null,
            in_stock: [],
            not_stocked: [
              candidateFixture({ part_id: 999, name: "470uF 25V", is_in_stock: false, qty_milli: 0 }),
            ],
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("100nF 0603 10% X7R\n470uF 25V ceramic capacitor");
    await screen.findByText("100nF 1206");
    fireEvent.click(screen.getByRole("button", { name: "Add all without parts" }));

    await waitFor(() => {
      expect(callsTo("/api/projects/1/bom").some((call) => call.method === "PUT")).toBe(true);
    });
    const put = callsTo("/api/projects/1/bom").find((call) => call.method === "PUT");
    const edits = put?.body["edits"] as { part_id: number | null }[];
    expect(edits.map((edit) => edit.part_id)).toEqual([null, null]);
    expect(edits).toEqual([
      { note: "100nF 0603 10% X7R", part_id: null, qty_per_assembly_milli: 3000 },
      // `required_milli: null` on this one — nothing said how many, so one per
      // assembly rather than a figure the user never gave.
      { note: "470uF 25V ceramic capacitor", part_id: null, qty_per_assembly_milli: 1000 },
    ]);
    // And the panel no longer confirms a not-stocked candidate while warning, in
    // the same render, that the user owns none of them.
    expect(screen.getByText("You own none of these")).toBeTruthy();
    // The assumed quantity is stated, not silent: line 1 said nothing about how
    // many, and 1 000 milli was written for it above.
    expect(
      screen.getByText(/does not say how many, so accepting it assumes one per assembly/),
    ).toBeTruthy();
  });

  it("sends qty_per_assembly_milli on every accept path, because a new line is a 422 without it", async () => {
    // `BomLineEdit._create_needs_a_quantity_delete_needs_an_id` rejects an edit
    // with no `id` and no quantity. It is a cross-field validator, so it is absent
    // from the generated `schema.ts` and `tsc` was happy — every accept in this
    // panel 422'd against the real app while these tests passed against a stub
    // returning 200.
    stubApi({
      suggestResponse: {
        lines: [
          suggestionLine({
            outcome: "stocked",
            in_stock: [candidateFixture({ part_id: 501, name: "10k 0603 resistor" })],
          }),
        ],
      },
    });
    renderScreen();

    await openPasteBox("3x 10k 1% 0603 resistor");
    await screen.findByText("10k 0603 resistor");

    // "Use this" — the one action that may attach a part, because a human picked
    // this specific candidate.
    fireEvent.click(screen.getByRole("button", { name: "Use this" }));
    await waitFor(() => {
      expect(callsTo("/api/projects/1/bom").some((call) => call.method === "PUT")).toBe(true);
    });
    const puts = callsTo("/api/projects/1/bom").filter((call) => call.method === "PUT");
    for (const put of puts) {
      for (const edit of put.body["edits"] as Record<string, unknown>[]) {
        expect(edit).not.toHaveProperty("id");
        expect(edit["qty_per_assembly_milli"]).toBe(3000);
      }
    }
    expect((puts[0]?.body["edits"] as { part_id: number | null }[])[0]?.part_id).toBe(501);
  });
});

describe("an undesignated line is flagged for cleanup", () => {
  it("marks a line with no designator the same way an unmatched line is marked", async () => {
    stubApi({ lines: [bomLine({ id: 9, line_no: 9, designators: null, part_id: 501, is_match_confirmed: true })] });
    renderScreen();

    expect(await screen.findByText("no designator")).toBeTruthy();
  });
});
