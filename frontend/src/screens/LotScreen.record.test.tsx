/**
 * Take, redirected into the focused tab's record — ADR 0010's behavioural core.
 *
 * The whole point is what does *not* happen: with a tab focused, pressing Take
 * writes nothing to the ledger. So every test here asserts against the requests
 * that were made, not only against the record, because "the line is in the cart"
 * would still pass if the screen had also silently consumed the stock.
 *
 * The two halves are tested against each other on purpose. Nothing open must keep
 * committing immediately — that is Iliana's separate standing request ("just pick a
 * container, scan it and say how many parts you took or put back"), not an edge case
 * of the other path.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { carts } from "../lib/cart/registry";
import { openTargets } from "../lib/projectcontext/store";
import { targetKey, type WorkTarget } from "../lib/projectcontext/target";
import { scanSession } from "../lib/scan/session";
import { LotScreen } from "./LotScreen";

const PROJECT: WorkTarget = { kind: "project", projectId: 12, label: "Bench PSU" };
const BUILD: WorkTarget = { kind: "build", buildId: 5, label: "rev B ×3" };
const OTHER_BUILD: WorkTarget = { kind: "build", buildId: 6, label: "rev C ×1" };

/** What `/api/projects/12` answers with — the chooser's list of iterations. */
let projectBuilds: Record<string, unknown>[] = [
  { id: 5, project_id: 12, build_no: 1, label: "rev B x3", assembly_count: 3, status: "planned" },
];

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
  readonly path: string;
  readonly method: string;
}

const calls: Call[] = [];

function stubApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      calls.push({ path: url.pathname, method: request.method });
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
        return json({ lot: { ...LOT, qty_milli: 1_198_000 }, seqs: [42], replayed: false });
      }
      if (url.pathname === "/api/stock/lots/7/history") {
        return json([]);
      }
      if (url.pathname === "/api/projects/12") {
        return json({
          id: 12,
          name: "Bench PSU",
          revision: "B",
          status: "planning",
          description: null,
          source_ref: null,
          notes: null,
          created_at: "2026-07-30T00:00:00Z",
          updated_at: "2026-07-30T00:00:00Z",
          builds: projectBuilds,
        });
      }
      if (url.pathname === "/api/projects/12/builds") {
        return json({
          replayed: false,
          build: {
            id: 9,
            project_id: 12,
            build_no: 2,
            label: null,
            assembly_count: 1,
            status: "planned",
          },
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

/** Anything that changes stock. A record-path take must make none of these. */
function writes(): Call[] {
  return calls.filter((call) => call.method !== "GET");
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

beforeEach(() => {
  calls.length = 0;
  projectBuilds = [
    { id: 5, project_id: 12, build_no: 1, label: "rev B x3", assembly_count: 3, status: "planned" },
  ];
  globalThis.localStorage.clear();
  carts.reset();
  openTargets.reset();
  scanSession.clear();
  stubApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("with a tab focused", () => {
  it("names the focused target on the button, so the attribution is visible", async () => {
    openTargets.openTarget(BUILD);
    renderScreen();

    // ADR 0010: a take must never be attributable to a target the user cannot see
    // named at the moment they press the button.
    expect(await screen.findByRole("button", { name: "Take 1 for rev B ×3" })).toBeTruthy();
  });

  it("adds a line to that tab's record and writes nothing", async () => {
    openTargets.openTarget(BUILD);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for rev B ×3" }));

    expect(carts.for(BUILD).lines()).toHaveLength(1);
    /*
     * `waitFor`, because the name is allowed to arrive late and by design does.
     * The take control is live before the part fetch lands, so a row can be added
     * carrying the `Lot 7` fallback and be relabelled a moment later — asserting
     * synchronously here passes or fails on which of the two won the race, which
     * is a coin toss under load rather than a statement about the screen.
     */
    await waitFor(() =>
      expect(carts.for(BUILD).lines()[0]).toMatchObject({
        partId: 3,
        partName: "22uF 25V ceramic, through-hole",
        mpn: "DEMO-CAP-THT-22U",
        qtyMilli: 1_000,
        lotId: 7,
        locationId: 11,
        locationLabel: "Cabinet A / Drawer A1",
        direction: "take",
      }),
    );
    expect(writes()).toEqual([]);
    expect(await screen.findByText(/Nothing has been written to the ledger/)).toBeTruthy();
  });

  it("leaves the scan's key unspent, because nothing was written", async () => {
    // Rule 4 in the screen's module comment: the key is spent *on use*, and a
    // deferred line is not a use. Spending it here would leave the take that
    // really does commit — after the tab is closed — minting a fresh key for a
    // scan whose key was thrown away.
    openTargets.openTarget(BUILD);
    const session = scanSession.scan("REEL", "DataMatrix");
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: /^Take 1 for/ }));

    expect(scanSession.current()?.clientOpId).toBe(session?.clientOpId);
  });

  it("nets a second take of the same bin into one line, under a fresh key", async () => {
    // Two takes in one session. The row's key is re-minted because the row is a
    // different statement now: reusing it would let the server replay the first
    // take's numbers and look exactly like a successful second one.
    openTargets.openTarget(BUILD);
    renderScreen();

    const button = await screen.findByRole("button", { name: /^Take 1 for/ });
    fireEvent.click(button);
    const first = carts.for(BUILD).lines()[0];
    fireEvent.click(button);

    const lines = carts.for(BUILD).lines();
    expect(lines).toHaveLength(1);
    expect(lines[0]?.qtyMilli).toBe(2_000);
    expect(lines[0]?.id).toBe(first?.id);
    expect(lines[0]?.clientOpId).not.toBe(first?.clientOpId);
    expect(writes()).toEqual([]);
  });

  it("makes a return a negative line in the same record, not a second row", async () => {
    // "I took four and put one back" is one activity and has to read as one.
    openTargets.openTarget(BUILD);
    renderScreen();

    fireEvent.click(await screen.findByLabelText("4"));
    fireEvent.click(screen.getByRole("button", { name: "Take 4 for rev B ×3" }));

    fireEvent.click(screen.getByRole("button", { name: "Return" }));
    fireEvent.click(await screen.findByLabelText("1"));
    fireEvent.click(screen.getByRole("button", { name: "Return 1 from rev B ×3" }));

    const lines = carts.for(BUILD).lines();
    expect(lines).toHaveLength(1);
    expect(lines[0]?.qtyMilli).toBe(3_000);
    expect(lines[0]?.direction).toBe("take");
    expect(writes()).toEqual([]);
  });

  it("undoes an uncommitted line for free, writing and reversing nothing", async () => {
    openTargets.openTarget(BUILD);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: /^Take 1 for/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Undo" }));

    expect(carts.for(BUILD).size).toBe(0);
    expect(writes()).toEqual([]);
  });
});

describe("with two tabs open", () => {
  it("puts the line in the focused one and leaves the other empty", async () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for rev B ×3" }));

    expect(carts.for(BUILD).size).toBe(1);
    expect(carts.for(PROJECT).size).toBe(0);
  });

  it("does not move existing lines when focus changes", async () => {
    // The reason the record is per-target at all: one shared list would silently
    // re-aim everything already gathered the moment the focused tab changed.
    openTargets.openTarget(OTHER_BUILD);
    openTargets.openTarget(BUILD);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for rev B ×3" }));
    const gathered = carts.for(BUILD).lines()[0]?.id;

    openTargets.focus(targetKey(OTHER_BUILD));
    // The button follows the focus, which is what makes the attribution honest.
    expect(await screen.findByRole("button", { name: "Take 1 for rev C ×1" })).toBeTruthy();

    expect(carts.for(BUILD).lines().map((line) => line.id)).toEqual([gathered]);
    expect(carts.for(OTHER_BUILD).size).toBe(0);

    fireEvent.click(screen.getByRole("button", { name: "Take 1 for rev C ×1" }));
    expect(carts.for(OTHER_BUILD).size).toBe(1);
    expect(carts.for(BUILD).lines().map((line) => line.id)).toEqual([gathered]);
    expect(writes()).toEqual([]);
  });
});

describe("with a project focused", () => {
  it("does not offer to attribute the take to the project", async () => {
    // ADR 0011: a project is the design. Naming it on the button would be the
    // control lying about where the parts are going.
    openTargets.openTarget(PROJECT);
    renderScreen();

    expect(await screen.findByRole("button", { name: "Take 1 for an iteration…" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Take 1 for Bench PSU" })).toBeNull();
  });

  it("asks which iteration, and writes nothing while it is asking", async () => {
    openTargets.openTarget(PROJECT);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for an iteration…" }));

    expect(await screen.findByText("Which iteration are these for?")).toBeTruthy();
    expect(await screen.findByRole("button", { name: /Build #1 — rev B x3/ })).toBeTruthy();
    expect(carts.for(PROJECT).size).toBe(0);
    expect(writes()).toEqual([]);
  });

  it("puts the line in the chosen iteration, and opens it as the focused tab", async () => {
    openTargets.openTarget(PROJECT);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for an iteration…" }));
    fireEvent.click(await screen.findByRole("button", { name: /Build #1 — rev B x3/ }));

    const chosen: WorkTarget = { kind: "build", buildId: 5, label: "Build #1 — rev B x3" };
    expect(carts.for(chosen).size).toBe(1);
    expect(carts.for(chosen).lines()[0]?.lotId).toBe(7);
    // The project itself gathers nothing: it is not somewhere parts can be.
    expect(carts.for(PROJECT).size).toBe(0);
    // Focused, so the next take on this screen goes straight there and the strip
    // says so without being asked again.
    expect(targetKey(openTargets.focused ?? PROJECT)).toBe(targetKey(chosen));
    expect(await screen.findByRole("button", { name: /^Take 1 for Build #1/ })).toBeTruthy();
    expect(writes()).toEqual([]);
  });

  it("can start an iteration when the project has none, and uses it", async () => {
    projectBuilds = [];
    openTargets.openTarget(PROJECT);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for an iteration…" }));
    fireEvent.click(await screen.findByRole("button", { name: /Start a new iteration/ }));

    const created: WorkTarget = { kind: "build", buildId: 9, label: "Build #2" };
    await waitFor(() => expect(carts.for(created).size).toBe(1));
    expect(targetKey(openTargets.focused ?? PROJECT)).toBe(targetKey(created));
  });

  it("does not offer an iteration that is finished", async () => {
    projectBuilds = [
      { id: 5, project_id: 12, build_no: 1, label: "done", assembly_count: 1, status: "completed" },
    ];
    openTargets.openTarget(PROJECT);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for an iteration…" }));

    expect(await screen.findByText(/One iteration is/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Build #1 — done/ })).toBeNull();
  });

  it("leaves the record alone when the question is cancelled", async () => {
    openTargets.openTarget(PROJECT);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1 for an iteration…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(screen.queryByText("Which iteration are these for?")).toBeNull();
    expect(carts.for(PROJECT).size).toBe(0);
    expect(writes()).toEqual([]);
  });
});

describe("with nothing open", () => {
  it("commits immediately, exactly as it always did", async () => {
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Take 1" }));

    await waitFor(() =>
      expect(calls.some((call) => call.path === "/api/stock/lots/7/consume")).toBe(true),
    );
    expect(await screen.findByText(/Took 1/)).toBeTruthy();
  });

  it("commits immediately again once the last tab is closed", async () => {
    // The transition is the interesting bit: the record path must not leave the
    // screen stuck in it.
    openTargets.openTarget(BUILD);
    renderScreen();
    expect(await screen.findByRole("button", { name: /^Take 1 for/ })).toBeTruthy();

    openTargets.close(targetKey(BUILD));

    fireEvent.click(await screen.findByRole("button", { name: "Take 1" }));
    await waitFor(() =>
      expect(calls.some((call) => call.path === "/api/stock/lots/7/consume")).toBe(true),
    );
  });
});
