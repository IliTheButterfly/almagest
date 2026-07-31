/**
 * The side panel — the thing ADR 0010 says the feature *is*.
 *
 * The tests are about its three mitigations rather than its layout, because the
 * layout is not what makes it safe. With several tabs open, the focused one
 * silently decides where a take is attributed, and the ADR is explicit that the
 * mitigations are cumulative and all required: the strip is always visible, a tab
 * holding uncommitted lines says so **even when it is not focused**, and closing
 * such a tab asks instead of discarding.
 *
 * The fourth thing checked here is the diff itself — "already in this" beside
 * "currently adding" — because being able to show only the second half is exactly
 * what made #40's cart the wrong shape.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { carts } from "../lib/cart/registry";
import { openTargets } from "../lib/projectcontext/store";
import { targetKey, type WorkTarget } from "../lib/projectcontext/target";
import { WorkPanel } from "./WorkPanel";

const PROJECT: WorkTarget = { kind: "project", projectId: 12, label: "Bench PSU" };
const BUILD: WorkTarget = { kind: "build", buildId: 5, label: "rev B ×3" };

interface Call {
  readonly path: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function shortageLine(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    allocated_milli: 0,
    available_milli: 0,
    bom_line_id: 1,
    consumed_milli: 0,
    is_blocking: false,
    kind: "component",
    line_no: 1,
    needed_milli: 0,
    part_id: 3,
    qty_per_assembly_milli: 1_000,
    required_milli: 1_000,
    reserved_milli: 0,
    shortfall_milli: 0,
    staged_milli: 0,
    substitute_part_ids: [],
    undeliverable_milli: 0,
    ...overrides,
  };
}

function bomLine(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    created_at: "2026-07-30T00:00:00Z",
    description: null,
    designators: "C1",
    footprint: null,
    id: 1,
    is_dnp: false,
    is_match_confirmed: true,
    line_no: 1,
    manufacturer_raw: null,
    mpn_norm: null,
    mpn_raw: "DEMO-CAP-THT-22U",
    note: null,
    part_id: 3,
    project_id: 12,
    qty_per_assembly_milli: 2_000,
    updated_at: "2026-07-30T00:00:00Z",
    ...overrides,
  };
}

/** Overridable per test: what each read answers with. */
let shortages: Record<string, unknown> = {
  assembly_count: 3,
  build_id: 5,
  is_buildable: false,
  lines: [
    shortageLine({ bom_line_id: 1, line_no: 1, reserved_milli: 4_000 }),
    shortageLine({ bom_line_id: 2, line_no: 2, staged_milli: 2_000, consumed_milli: 1_000 }),
    shortageLine({ bom_line_id: 3, line_no: 3, needed_milli: 6_000, is_blocking: true }),
  ],
};
let bom: Record<string, unknown> = { lines: [bomLine()], total: 1 };
/** The build read the commit makes on its way to attributing a staged part. */
const BUILD_READ = {
  id: 5,
  project_id: 12,
  build_no: 2,
  label: "rev B x3",
  assembly_count: 3,
  bom_revision: "B",
  status: "planned",
  staging_location_id: null,
  started_at: null,
  completed_at: null,
  notes: null,
  created_at: "2026-07-30T00:00:00Z",
  updated_at: "2026-07-30T00:00:00Z",
};

/** Per test: what each `/stage` call answers, by the lot it names. */
let stageReply: (lotId: number) => Response = () =>
  new Response(
    JSON.stringify({ replayed: false, staging_location_id: 77, seqs: [1] }),
    { status: 200, headers: { "content-type": "application/json" } },
  );

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

      if (url.pathname === "/api/builds/5/shortages") {
        return json(shortages);
      }
      if (url.pathname === "/api/projects/12/bom") {
        return json(bom);
      }
      if (url.pathname === "/api/builds/5/stage") {
        return stageReply(
          (JSON.parse(raw) as { lot_id: number }).lot_id,
        );
      }
      if (url.pathname === "/api/builds/5") {
        return json(BUILD_READ);
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderPanel() {
  return render(
    <MemoryRouter>
      <WorkPanel />
    </MemoryRouter>,
  );
}

/** A gathered line, added the way the take screen adds one. */
function gather(target: WorkTarget, overrides: Record<string, unknown> = {}): void {
  carts.for(target).add({
    partId: 3,
    partName: "22uF 25V ceramic, through-hole",
    mpn: "DEMO-CAP-THT-22U",
    qtyMilli: 4_000,
    lotId: 7,
    locationId: 11,
    locationLabel: "Cabinet A / Drawer A1",
    direction: "take",
    ...overrides,
  });
}

beforeEach(() => {
  calls.length = 0;
  globalThis.localStorage.clear();
  carts.reset();
  openTargets.reset();
  shortages = {
    assembly_count: 3,
    build_id: 5,
    is_buildable: false,
    lines: [
      shortageLine({ bom_line_id: 1, line_no: 1, reserved_milli: 4_000 }),
      shortageLine({ bom_line_id: 2, line_no: 2, staged_milli: 2_000, consumed_milli: 1_000 }),
      shortageLine({ bom_line_id: 3, line_no: 3, needed_milli: 6_000, is_blocking: true }),
    ],
  };
  bom = { lines: [bomLine()], total: 1 };
  stageReply = () =>
    new Response(
      JSON.stringify({ replayed: false, staging_location_id: 77, seqs: [1] }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  stubApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("with nothing open", () => {
  it("draws nothing at all, and asks the server nothing", () => {
    const { container } = renderPanel();
    expect(container.textContent).toBe("");
    expect(calls).toEqual([]);
  });
});

describe("the tab strip", () => {
  it("shows one tab per open target and marks the focused one", async () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    renderPanel();

    const project = await screen.findByRole("tab", { name: /Bench PSU/ });
    const build = screen.getByRole("tab", { name: /rev B ×3/ });
    // Opening focuses, so the second one opened is the focused one.
    expect(project.getAttribute("aria-selected")).toBe("false");
    expect(build.getAttribute("aria-selected")).toBe("true");
  });

  it("moves focus, which is what decides where the next take goes", async () => {
    openTargets.openTarget(PROJECT);
    openTargets.openTarget(BUILD);
    renderPanel();

    fireEvent.click(await screen.findByRole("tab", { name: /Bench PSU/ }));

    expect(openTargets.focused).toEqual(PROJECT);
    expect(screen.getByRole("tab", { name: /Bench PSU/ }).getAttribute("aria-selected")).toBe(
      "true",
    );
  });

  it("marks an unfocused tab that is holding uncommitted lines", async () => {
    // ADR 0010: otherwise the second tab is exactly the invisible state the panel
    // exists to prevent.
    openTargets.openTarget(PROJECT);
    gather(PROJECT);
    gather(PROJECT, { partId: 4, lotId: 8, partName: "1k 1% 0603" });
    openTargets.openTarget(BUILD);
    renderPanel();

    const project = await screen.findByRole("tab", { name: /Bench PSU/ });
    expect(project.getAttribute("aria-selected")).toBe("false");
    expect(project.textContent).toContain("2");
  });
});

describe("closing a tab", () => {
  it("closes an empty one without asking", async () => {
    openTargets.openTarget(BUILD);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Close rev B ×3" }));

    expect(openTargets.open()).toEqual([]);
  });

  it("refuses to discard gathered lines silently, and says how many", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Close rev B ×3" }));

    expect(openTargets.isOpen(BUILD)).toBe(true);
    expect(screen.getByText(/One line/)).toBeTruthy();
    expect(carts.for(BUILD).size).toBe(1);
  });

  it("discards them only on a second, deliberate press", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Close rev B ×3" }));
    fireEvent.click(screen.getByRole("button", { name: /Discard it and close/ }));

    expect(openTargets.isOpen(BUILD)).toBe(false);
    // Cleared as well: rows left behind would resurrect the next time the target
    // was opened, long after the user said to throw them away.
    expect(carts.for(BUILD).size).toBe(0);
  });

  it("keeps the tab when the answer is no", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Close rev B ×3" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep the tab" }));

    expect(openTargets.isOpen(BUILD)).toBe(true);
    expect(carts.for(BUILD).size).toBe(1);
  });
});

describe("already in this, beside what is being added", () => {
  it("shows a build's held, set aside and built in — the derived three", async () => {
    openTargets.openTarget(BUILD);
    renderPanel();

    expect(await screen.findByText("held in a bin")).toBeTruthy();
    expect(screen.getByText("set aside for this project")).toBeTruthy();
    expect(screen.getByText("built in")).toBeTruthy();
    expect(screen.getByText(/3 assemblies/)).toBeTruthy();
    expect(screen.getByText(/not yet buildable/)).toBeTruthy();
  });

  it("says how many lines it is not showing rather than ending silently", async () => {
    shortages = {
      assembly_count: 1,
      build_id: 5,
      is_buildable: false,
      lines: Array.from({ length: 11 }, (_unused, index) =>
        shortageLine({ bom_line_id: index + 1, line_no: index + 1, needed_milli: 1_000 }),
      ),
    };
    openTargets.openTarget(BUILD);
    renderPanel();

    expect(await screen.findByText(/3 more lines not shown here/)).toBeTruthy();
  });

  it("shows a project's bill of materials, which is what its record commits to", async () => {
    openTargets.openTarget(PROJECT);
    renderPanel();

    expect(await screen.findByText("DEMO-CAP-THT-22U")).toBeTruthy();
    expect(screen.getByText(/One line/)).toBeTruthy();
    expect(calls.map((call) => call.path)).toContain("/api/projects/12/bom");
  });

  it("lists what is being added, and says nothing has been written", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();

    expect(await screen.findByText("22uF 25V ceramic, through-hole")).toBeTruthy();
    expect(screen.getByText(/Cabinet A \/ Drawer A1/)).toBeTruthy();
  });

  it("says so plainly when a tab has gathered nothing yet", async () => {
    openTargets.openTarget(BUILD);
    renderPanel();

    expect(await screen.findByText(/Taking from a lot while this tab is focused/)).toBeTruthy();
  });

  it("draws the focused tab's record, not another tab's", async () => {
    openTargets.openTarget(PROJECT);
    gather(PROJECT, { partName: "only in the project" });
    openTargets.openTarget(BUILD);
    gather(BUILD, { partName: "only in the build" });
    renderPanel();

    expect(await screen.findByText("only in the build")).toBeTruthy();
    expect(screen.queryByText("only in the project")).toBeNull();
  });

  it("removes a row without writing anything", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();

    fireEvent.click(
      await screen.findByRole("button", { name: "Remove 22uF 25V ceramic, through-hole" }),
    );

    expect(carts.for(BUILD).size).toBe(0);
    expect(calls.filter((call) => call.method !== "GET")).toEqual([]);
  });
});

describe("committing a tab", () => {
  it("moves the parts into that tab's target and drops the applied rows", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Send these parts to/ }));

    await waitFor(() =>
      expect(calls.some((call) => call.path === "/api/builds/5/stage")).toBe(true),
    );
    await waitFor(() => expect(carts.for(BUILD).size).toBe(0));
    expect(await screen.findByText(/One line committed/)).toBeTruthy();
  });

  it("carries the key the line was minted with, so a resend replays", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    const line = carts.for(BUILD).lines()[0];
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Send these parts to/ }));

    await waitFor(() =>
      expect(calls.some((call) => call.path === "/api/builds/5/stage")).toBe(true),
    );
    const sent = calls.find((call) => call.path === "/api/builds/5/stage");
    expect(sent?.body["client_op_id"]).toBe(line?.clientOpId);
  });

  it("keeps a refused line, with the reason, and does not lose the rest", async () => {
    // The rule the whole feature rests on: a line whose stock has moved fails
    // *that* line, and the record itself then explains why it is not empty.
    stageReply = (lotId) =>
      lotId === 8
        ? new Response(
            JSON.stringify({
              detail: { reason: "insufficient_stock", message: "That lot only has 1 free." },
            }),
            { status: 409, headers: { "content-type": "application/json" } },
          )
        : new Response(
            JSON.stringify({ replayed: false, staging_location_id: 77, seqs: [1] }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
    openTargets.openTarget(BUILD);
    gather(BUILD);
    gather(BUILD, { partId: 4, lotId: 8, partName: "1k 1% 0603" });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Send these parts to/ }));

    await waitFor(() => expect(carts.for(BUILD).size).toBe(1));
    expect(carts.for(BUILD).lines()[0]?.partName).toBe("1k 1% 0603");
    expect(await screen.findByText(/That lot only has 1 free/)).toBeTruthy();
    expect(screen.getByText(/still\s+here with the reason on the row/)).toBeTruthy();
  });

  it("re-reads what is already in the target once lines have been applied", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();
    await screen.findByText("held in a bin");
    const before = calls.filter((call) => call.path === "/api/builds/5/shortages").length;

    fireEvent.click(screen.getByRole("button", { name: /Send these parts to/ }));

    await waitFor(() =>
      expect(
        calls.filter((call) => call.path === "/api/builds/5/shortages").length,
      ).toBeGreaterThan(before),
    );
  });

  it("refuses a row that nets to putting stock back, without sending it", async () => {
    // Neither destination has a negative: a BOM line asks for a quantity and an
    // allocation is a hold. Refused locally, with the way out named.
    openTargets.openTarget(BUILD);
    gather(BUILD, { direction: "return" });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Send these parts to/ }));

    await waitFor(() => expect(screen.getByText(/nets to putting stock back/)).toBeTruthy());
    expect(calls.some((call) => call.path === "/api/builds/5/stage")).toBe(false);
    expect(carts.for(BUILD).size).toBe(1);
  });

  it("commits the focused tab and leaves the other tab's record alone", async () => {
    openTargets.openTarget(PROJECT);
    gather(PROJECT);
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /Send these parts to/ }));

    await waitFor(() => expect(carts.for(BUILD).size).toBe(0));
    expect(carts.for(PROJECT).size).toBe(1);
    expect(calls.some((call) => call.path === "/api/projects/12/bom" && call.method !== "GET")).toBe(
      false,
    );
  });
});

describe("switching tabs", () => {
  it("reads the newly focused target's record rather than reusing the last one", async () => {
    openTargets.openTarget(BUILD);
    openTargets.openTarget(PROJECT);
    renderPanel();
    await screen.findByText("DEMO-CAP-THT-22U");

    fireEvent.click(screen.getByRole("tab", { name: /rev B ×3/ }));

    expect(await screen.findByText("held in a bin")).toBeTruthy();
    expect(screen.queryByText("DEMO-CAP-THT-22U")).toBeNull();
  });

  it("names the focused target on the commit button, so the destination is visible", async () => {
    openTargets.openTarget(PROJECT);
    gather(PROJECT);
    renderPanel();

    expect(
      await screen.findByRole("button", { name: /bill of materials of Bench PSU/ }),
    ).toBeTruthy();
  });
});

describe("collapsing", () => {
  it("hides a section's body but never the strip", async () => {
    openTargets.openTarget(BUILD);
    gather(BUILD);
    renderPanel();
    await screen.findByText("22uF 25V ceramic, through-hole");

    const toggles = screen.getAllByRole("button", { name: "Hide" });
    fireEvent.click(toggles[toggles.length - 1] as HTMLElement);

    expect(screen.queryByText("22uF 25V ceramic, through-hole")).toBeNull();
    // The mitigation stays on screen: the strip is not collapsible.
    expect(screen.getByRole("tab", { name: /rev B ×3/ })).toBeTruthy();
    expect(targetKey(openTargets.focused ?? PROJECT)).toBe(targetKey(BUILD));
  });
});
