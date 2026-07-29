/**
 * The build screen's two new tabs, against a stubbed `fetch`.
 *
 * Both features are worthless if they are merely plausible, so these tests pin
 * the two things a well-meaning refactor destroys silently:
 *
 * * **the pick list is rendered in the order the server sent it.** That order is
 *   `locations.id_path`, which groups a cabinet's drawers together; the stub
 *   deliberately sends stops whose BOM line numbers run *backwards*, so a
 *   component that sorted by line number — the obvious, wrong thing — flips the
 *   list and the assertion fails. There is no way to write that assertion by
 *   accident;
 * * **an after-the-fact roster row reads differently from a tracked one.** Both
 *   say "consumed 2" and both are true; only one was witnessed. The test asserts
 *   the difference survives in words, not only in a class name, because a
 *   colour-only distinction is invisible to a third of the reasons someone reads
 *   a roster at all.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BuildScreen } from "./BuildScreen";

const BUILD = {
  id: 5,
  project_id: 1,
  build_no: 2,
  label: "Prototype run",
  assembly_count: 3,
  bom_revision: "B",
  status: "in_progress",
  started_at: null,
  completed_at: null,
  notes: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

/**
 * Three stops in `id_path` order — Cabinet A's two drawers together, then
 * Cabinet B — carrying line numbers 3, 2, 1. Exactly the shape
 * `test_stops_are_ordered_for_walking_and_not_by_bom_line` builds on the server:
 * BOM order is the *reverse* of walking order here, so the two cannot be confused.
 */
const STOPS = [
  {
    location_id: 11,
    label_path: "Cabinet A / Drawer A1",
    id_path: "/1/11/",
    short_id: "4K7T-92M8",
    qty_milli: 3_000,
    takes: [
      {
        bom_line_id: 30,
        line_no: 3,
        designators: "R7",
        part_id: 43,
        part_name: "10k 0603 resistor",
        part_mpn: "RC0603FR-0710KL",
        lot_id: 900,
        qty_milli: 3_000,
        allocation_id: null,
        is_substitute: false,
        whole_lot: true,
      },
    ],
  },
  {
    location_id: 12,
    label_path: "Cabinet A / Drawer A2",
    id_path: "/1/12/",
    short_id: null,
    qty_milli: 6_000,
    takes: [
      {
        bom_line_id: 20,
        line_no: 2,
        designators: "C1, C2",
        part_id: 44,
        part_name: "100n 0603 capacitor",
        part_mpn: null,
        lot_id: 901,
        qty_milli: 6_000,
        allocation_id: 77,
        is_substitute: true,
        whole_lot: false,
      },
    ],
  },
  {
    location_id: 21,
    label_path: "Cabinet B / Drawer B1",
    id_path: "/2/21/",
    short_id: null,
    qty_milli: 1_000,
    takes: [
      {
        bom_line_id: 10,
        line_no: 1,
        designators: "U1",
        part_id: 45,
        part_name: "the far part",
        part_mpn: null,
        lot_id: 902,
        qty_milli: 1_000,
        allocation_id: null,
        is_substitute: false,
        whole_lot: false,
      },
    ],
  },
];

const UNMATCHED_GAP = {
  bom_line_id: 40,
  line_no: 4,
  part_id: null,
  kind: "unidentified",
  needed_milli: 4_000,
  pickable_milli: 0,
  shortfall_milli: 4_000,
};

const SHORT_GAP = {
  bom_line_id: 50,
  line_no: 5,
  part_id: 46,
  kind: "short",
  needed_milli: 10_000,
  pickable_milli: 4_000,
  shortfall_milli: 6_000,
};

const TRACKED_ENTRY = {
  allocation_id: 1,
  part_id: 43,
  part_name: "10k 0603 resistor",
  part_mpn: "RC0603FR-0710KL",
  lot_id: 900,
  qty_milli: 2_000,
  state: "consumed",
  ledger_seq: 500,
  ledger_source: "manual",
  is_after_the_fact: false,
  location_id: 11,
  location_label_path: "Cabinet A / Drawer A1",
  reserved_at: "2026-01-01T00:00:00Z",
  consumed_at: "2026-01-02T00:00:00Z",
  note: null,
};

const CORRECTED_ENTRY = {
  ...TRACKED_ENTRY,
  allocation_id: 2,
  ledger_seq: 501,
  ledger_source: "reconciled",
  is_after_the_fact: true,
  reserved_at: null,
  note: "found two missing when the board was finished",
};

const PLANNED_ROSTER_LINE = {
  bom_line_id: 30,
  line_no: 3,
  designators: "R7",
  part_id: 43,
  part_name: "10k 0603 resistor",
  part_mpn: "RC0603FR-0710KL",
  is_dnp: false,
  is_off_bom: false,
  required_milli: 9_000,
  reserved_milli: 0,
  staged_milli: 0,
  consumed_milli: 4_000,
  after_the_fact_milli: 2_000,
  entries: [TRACKED_ENTRY, CORRECTED_ENTRY],
};

const OFF_BOM_LINE = {
  bom_line_id: null,
  line_no: null,
  designators: null,
  part_id: 99,
  part_name: "the fix nobody drew",
  part_mpn: "1N4148",
  is_dnp: false,
  is_off_bom: true,
  required_milli: 0,
  reserved_milli: 0,
  staged_milli: 0,
  consumed_milli: 1_000,
  after_the_fact_milli: 1_000,
  entries: [
    {
      ...CORRECTED_ENTRY,
      allocation_id: 3,
      part_id: 99,
      part_name: "the fix nobody drew",
      qty_milli: 1_000,
      note: null,
    },
  ],
};

/** A line nothing has happened to. Listed anyway: a roster showing only the
 * lines with activity reads as complete while half the board is unaccounted for. */
const UNTOUCHED_ROSTER_LINE = {
  ...PLANNED_ROSTER_LINE,
  bom_line_id: 20,
  line_no: 2,
  designators: "C1, C2",
  part_id: 44,
  part_name: "100n 0603 capacitor",
  part_mpn: null,
  required_milli: 6_000,
  consumed_milli: 0,
  after_the_fact_milli: 0,
  entries: [],
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(
  options: {
    stops?: readonly unknown[];
    gaps?: readonly unknown[];
    rosterLines?: readonly unknown[];
    afterTheFactMilli?: number;
  } = {},
): void {
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

      if (url.pathname === "/api/builds/5" && request.method === "GET") {
        return json(BUILD);
      }
      if (url.pathname === "/api/builds/5/shortages") {
        return json({ build_id: 5, assembly_count: 3, is_buildable: true, lines: [] });
      }
      if (url.pathname === "/api/builds/5/pick-list") {
        const stops = options.stops ?? STOPS;
        const gaps = options.gaps ?? [];
        return json({
          build_id: 5,
          is_complete: gaps.length === 0,
          qty_milli: 10_000,
          stops,
          gaps,
        });
      }
      if (url.pathname === "/api/builds/5/roster") {
        const lines = options.rosterLines ?? [PLANNED_ROSTER_LINE, OFF_BOM_LINE];
        return json({
          build_id: 5,
          assembly_count: 3,
          after_the_fact_milli: options.afterTheFactMilli ?? 3_000,
          off_bom_count: lines.filter((line) => (line as { is_off_bom: boolean }).is_off_bom).length,
          lines,
        });
      }
      if (url.pathname === "/api/builds/5/record-used") {
        return json({
          allocation: CORRECTED_ENTRY,
          lot: {
            id: 900,
            part_id: 43,
            location_id: 11,
            qty_milli: 8_000,
            qty_reserved_milli: 0,
            status: "active",
          },
          seq: 502,
          replayed: false,
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/builds/5"]}>
      <Routes>
        <Route path="/builds/:buildId" element={<BuildScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** The first match, asserted to exist. `?.` here would let a missing element
 * pass the assertion that follows, which is the one failure mode a test must
 * never have. */
function first<T>(items: readonly T[]): T {
  const found = items[0];
  if (found === undefined) {
    throw new Error("expected at least one match");
  }
  return found;
}

async function openTab(name: "Pick list" | "Roster"): Promise<void> {
  fireEvent.click(await screen.findByRole("button", { name }));
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the pick list's walking order", () => {
  /** **This assertion is the feature.** */
  it("renders the stops in server order, which is walking order and not BOM order", async () => {
    stubApi();
    renderScreen();
    await openTab("Pick list");

    const items = await screen.findAllByRole("listitem");
    const paths = items
      .map((item) => item.textContent ?? "")
      .filter((text) => text.includes("Cabinet"));

    // Cabinet A's two drawers are consecutive and Cabinet B comes last — one lap
    // of the room. The stub's line numbers run 3, 2, 1 across these three stops,
    // so sorting by BOM line would reverse this list.
    expect(paths[0]).toContain("Cabinet A / Drawer A1");
    expect(paths[1]).toContain("Cabinet A / Drawer A2");
    expect(paths[2]).toContain("Cabinet B / Drawer B1");
  });

  it("numbers the stops in that order, so the order is stated and not just implied", async () => {
    stubApi();
    renderScreen();
    await openTab("Pick list");

    expect(await screen.findByText(/^1\.$/)).toBeTruthy();
    expect(first(await screen.findAllByRole("listitem")).textContent).toContain(
      "Cabinet A / Drawer A1",
    );
  });

  it("says how much to take from each bin rather than only naming the bins", async () => {
    stubApi();
    renderScreen();
    await openTab("Pick list");

    // "It is in these three drawers" is not an instruction; a quantity per bin is.
    // Each stop must carry *its own* number, so the three are deliberately
    // distinct (3, 6, 1) and each is looked for inside its own stop.
    await screen.findByText("Cabinet A / Drawer A1");
    const items = await screen.findAllByRole("listitem");
    const quantityIn = (label: string): string | undefined =>
      items
        .find((item) => item.textContent?.includes(label))
        ?.querySelector(".big-number")?.textContent ?? undefined;

    expect(quantityIn("Cabinet A / Drawer A1")).toBe("3");
    expect(quantityIn("Cabinet A / Drawer A2")).toBe("6");
    expect(quantityIn("Cabinet B / Drawer B1")).toBe("1");
    // …and the per-take line repeats it beside the part, because the stop total is
    // not enough once a bin supplies two different lines.
    const a2 = items.find((item) => item.textContent?.includes("Drawer A2"));
    expect(a2?.textContent).toContain("6 of 100n 0603 capacitor");
  });

  it("flags a whole-lot take, a substitute and an existing hold in words", async () => {
    stubApi();
    renderScreen();
    await openTab("Pick list");

    // Each changes what the hand does at the drawer, so each is a word and a
    // glyph rather than a shade of the row.
    expect(await screen.findByText("whole lot")).toBeTruthy();
    expect(screen.getByText("substitute")).toBeTruthy();
    expect(screen.getByText("already held")).toBeTruthy();
  });

  it("offers the printed bin code where there is one and invents none where there is not", async () => {
    stubApi();
    renderScreen();
    await openTab("Pick list");

    expect(await screen.findByText("4K7T-92M8")).toBeTruthy();
    // Drawer A2 has no printed code; most generated grid cells never will.
    const items = await screen.findAllByRole("listitem");
    const a2 = items.find((item) => item.textContent?.includes("Drawer A2"));
    expect(a2?.textContent).not.toContain("4K7T");
  });
});

describe("a line the walk cannot finish", () => {
  it("shows an unmatched line as having nothing to look for, never as zero short", async () => {
    stubApi({ gaps: [UNMATCHED_GAP] });
    renderScreen();
    await openTab("Pick list");

    expect(await screen.findByText("nothing to look for")).toBeTruthy();
    expect(screen.getByText(/No part is matched to this line/)).toBeTruthy();
    // A quantity here would be a fabrication: there is no part to price or count.
    expect(screen.queryByText(/not in stock anywhere/)).toBeNull();
  });

  it("gives an unmatched line a different badge and next action from a short one", async () => {
    stubApi({ gaps: [UNMATCHED_GAP, SHORT_GAP] });
    renderScreen();
    await openTab("Pick list");

    const unmatched = await screen.findByText("nothing to look for");
    const short = screen.getByText("not enough in stock");
    expect(unmatched.className).toContain("badge-warn");
    expect(short.className).toContain("badge-bad");
    expect(unmatched.textContent).not.toBe(short.textContent);
    // One is fixed by a human saying what the part is, the other by ordering.
    expect(screen.getByText(/Match it in the BOM/)).toBeTruthy();
    expect(screen.getByText(/6 not in stock anywhere/)).toBeTruthy();
  });

  it("warns that a walk with gaps will not finish the build", async () => {
    stubApi({ gaps: [SHORT_GAP] });
    renderScreen();
    await openTab("Pick list");

    expect(await screen.findByText(/1 line\(s\) cannot be fully picked/)).toBeTruthy();
    expect(screen.getByText(/will not finish the build/)).toBeTruthy();
  });

  it("says a partly-pickable line is only partly on the walk", async () => {
    stubApi({ gaps: [SHORT_GAP] });
    renderScreen();
    await openTab("Pick list");

    // The case a pick list lies about most easily: it lists the takes it can and
    // says nothing about the rest.
    expect(await screen.findByText(/do not read those stops as finishing it/)).toBeTruthy();
  });

  it("distinguishes an empty walk from a complete one", async () => {
    stubApi({ stops: [], gaps: [] });
    renderScreen();
    await openTab("Pick list");

    expect(await screen.findByText("Nothing to fetch")).toBeTruthy();
    expect(screen.queryByText(/cannot be fully picked/)).toBeNull();
  });
});

describe("the roster's honesty about its own edits", () => {
  /** **The requirement.** */
  it("reads an after-the-fact row differently from a tracked one, in words", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    // Two rows on one line, both "consumed 2", both true. Only one was observed.
    expect(await screen.findByText("built in")).toBeTruthy();
    const corrections = screen.getAllByText("recorded after the fact");
    expect(corrections.length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Entered by hand, not captured as it happened/).length).toBe(
      corrections.length,
    );
  });

  it("carries the distinction on a third channel as well as the words", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    const correction = first(await screen.findAllByText("recorded after the fact"));
    const tracked = screen.getByText("built in");
    // badge-warn brings its own glyph and a heavier border, so the difference
    // survives greyscale and a colour-blind reader.
    expect(correction.className).toContain("badge-warn");
    expect(tracked.className).not.toContain("badge-warn");
  });

  it("states at the top that part of the roster was reconstructed", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    expect(
      await screen.findByText(/Part of this roster was entered after the fact/),
    ).toBeTruthy();
  });

  it("says nothing of the sort when every row was captured as it happened", async () => {
    stubApi({
      rosterLines: [{ ...PLANNED_ROSTER_LINE, after_the_fact_milli: 0, entries: [TRACKED_ENTRY] }],
      afterTheFactMilli: 0,
    });
    renderScreen();
    await openTab("Roster");

    await screen.findByText("built in");
    expect(screen.queryByText(/entered after the fact/)).toBeNull();
    expect(screen.queryByText("recorded after the fact")).toBeNull();
  });

  it("keeps planned, held, set aside and built-in as four numbers", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    // Adding these up is what makes a build look accounted-for off parts that are
    // still in a bin, so the row names all four rather than showing progress.
    const line = await screen.findByText(/planned \(×3\)/);
    expect(line.textContent).toContain("held");
    expect(line.textContent).toContain("set aside");
    expect(line.textContent).toContain("built in");
  });

  it("lists a line nothing has happened to instead of hiding it", async () => {
    stubApi({ rosterLines: [PLANNED_ROSTER_LINE, UNTOUCHED_ROSTER_LINE] });
    renderScreen();
    await openTab("Roster");

    expect(await screen.findByText("Line 2")).toBeTruthy();
  });
});

describe("parts used that the BOM never asked for", () => {
  it("gives them their own section and calls the BOM stale rather than the entry wrong", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    expect(await screen.findByText("Used but not on the BOM")).toBeTruthy();
    expect(screen.getByText("the fix nobody drew")).toBeTruthy();
    expect(screen.getByText(/the usual signal that the BOM is behind the hardware/)).toBeTruthy();
  });

  it("says so plainly when there are none", async () => {
    stubApi({ rosterLines: [PLANNED_ROSTER_LINE] });
    renderScreen();
    await openTab("Roster");

    await screen.findByText("Used but not on the BOM");
    expect(screen.getByText("Nothing so far.")).toBeTruthy();
    expect(screen.queryByText(/the BOM does not list/)).toBeNull();
  });
});

describe("recording a part that was really used", () => {
  it("posts it against the line, with no source field for the client to choose", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    fireEvent.click(first(await screen.findAllByRole("button", { name: "Record what was used" })));
    fireEvent.change(await screen.findByPlaceholderText("lot id"), { target: { value: "900" } });
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Record as used" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/builds/5/record-used")).toBe(true),
    );
    const post = calls.find((call) => call.url === "/api/builds/5/record-used");
    expect(post?.body["lot_id"]).toBe(900);
    expect(post?.body["qty_milli"]).toBe(2_000);
    expect(post?.body["bom_line_id"]).toBe(30);
    // The server forces `reconciled`. A client able to label a correction a scan
    // would erase the one property that makes the roster worth reading.
    expect(post?.body["source"]).toBeUndefined();
    // Idempotency-guarded: an append-only ledger can only take back a doubled
    // correction by writing a third row.
    expect(typeof post?.body["client_op_id"]).toBe("string");
  });

  it("posts a null bom_line_id for the part nobody planned for", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    fireEvent.click(
      await screen.findByRole("button", { name: "Record a part nobody planned for" }),
    );
    fireEvent.change(await screen.findByPlaceholderText("lot id"), { target: { value: "902" } });
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Record as used" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/builds/5/record-used")).toBe(true),
    );
    // Not a synthetic BOM line: that would put a component on the BOM that the
    // board does not have.
    expect(calls.find((call) => call.url === "/api/builds/5/record-used")?.body["bom_line_id"]).toBe(
      null,
    );
  });

  it("warns that the drawer's count drops before anything is posted", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    fireEvent.click(first(await screen.findAllByRole("button", { name: "Record what was used" })));

    expect(await screen.findByText(/This is a correction, and it will say so/)).toBeTruthy();
    expect(screen.getByText(/permanently marked as entered by hand/)).toBeTruthy();
    expect(calls.some((call) => call.url === "/api/builds/5/record-used")).toBe(false);
  });

  it("refuses to submit without a lot and a quantity, and says why rather than doing nothing", async () => {
    stubApi();
    renderScreen();
    await openTab("Roster");

    fireEvent.click(first(await screen.findAllByRole("button", { name: "Record what was used" })));
    const submit = await screen.findByRole("button", { name: "Record as used" });
    expect(submit.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/Enter the lot it came from and a quantity above zero/)).toBeTruthy();

    // Filling in only the lot narrows the reason down to the quantity.
    fireEvent.change(await screen.findByPlaceholderText("lot id"), { target: { value: "900" } });
    expect(screen.getByText(/Enter a quantity above zero\./)).toBeTruthy();
    expect(submit.hasAttribute("disabled")).toBe(true);
    expect(calls.some((call) => call.url === "/api/builds/5/record-used")).toBe(false);
  });
});
