/**
 * The build screen's shortage report, exercised against a stubbed `fetch`.
 *
 * The behaviour under test is the one the task exists to prevent: a `SHORT`
 * line (a known part, a known deficit — fixed by ordering) and an
 * `UNIDENTIFIED` line (no part named at all — fixed by a human, not a
 * purchase order) must not read the same, and a build with either present
 * must not claim to be buildable.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BuildScreen } from "./BuildScreen";

const BUILD = {
  id: 5,
  project_id: 1,
  build_no: 1,
  label: "Prototype run",
  assembly_count: 3,
  bom_revision: "B",
  status: "planned",
  started_at: null,
  completed_at: null,
  notes: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const SHORT_LINE = {
  bom_line_id: 10,
  line_no: 1,
  part_id: 42,
  kind: "short",
  required_milli: 30_000,
  allocated_milli: 10_000,
  available_milli: 5_000,
  shortfall_milli: 15_000,
  undeliverable_milli: 0,
  substitute_part_ids: [],
  is_blocking: true,
};

const UNIDENTIFIED_LINE = {
  bom_line_id: 11,
  line_no: 2,
  part_id: null,
  kind: "unidentified",
  required_milli: 3_000,
  allocated_milli: 0,
  available_milli: null,
  shortfall_milli: null,
  undeliverable_milli: 0,
  substitute_part_ids: [],
  is_blocking: true,
};

const SATISFIED_LINE = {
  bom_line_id: 12,
  line_no: 3,
  part_id: 43,
  kind: "satisfied",
  required_milli: 3_000,
  allocated_milli: 3_000,
  available_milli: 100_000,
  shortfall_milli: 0,
  undeliverable_milli: 0,
  substitute_part_ids: [],
  is_blocking: false,
};

const PART = {
  id: 42,
  name: "10k 0603 resistor",
  mpn: "RC0603FR-0710KL",
  description: null,
  is_stub: false,
  is_active: true,
  category_id: null,
  manufacturer_id: null,
  package_type_id: null,
  part_kind: "component",
  keywords: null,
  notes: null,
  mpn_norm: "RC0603FR0710KL",
  short_id: null,
  hot_score: 0,
  total_qty_milli: 5_000,
  unit_mass_mg: null,
  unit_volume_mm3: null,
  uom_id: null,
  volume_source: null,
  length_mm: null,
  width_mm: null,
  height_mm: null,
  shape_factor: null,
  lots: [
    {
      id: 900,
      part_id: 42,
      location_id: 1,
      qty_milli: 5_000,
      qty_reserved_milli: 0,
      status: "active",
      location_label_path: "Cabinet A / Bin 3",
    },
  ],
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(
  options: { lines?: readonly unknown[]; isBuildable?: boolean } = {},
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
        const lines = options.lines ?? [SHORT_LINE, UNIDENTIFIED_LINE];
        return json({
          build_id: 5,
          assembly_count: 3,
          is_buildable: options.isBuildable ?? false,
          lines,
        });
      }
      if (url.pathname === "/api/parts/42") {
        return json(PART);
      }
      if (url.pathname === "/api/builds/5/allocate") {
        return json({
          allocation: {
            id: 1,
            build_id: 5,
            bom_line_id: 10,
            part_id: 42,
            lot_id: 900,
            qty_milli: 15_000,
            state: "reserved",
            consumed_ledger_seq: null,
            reserved_at: "2026-01-01T00:00:00Z",
            consumed_at: null,
            note: null,
          },
          lot: { id: 900, part_id: 42, location_id: 1, qty_milli: 5_000, qty_reserved_milli: 15_000, status: "active" },
          replayed: false,
        });
      }
      if (url.pathname === "/api/builds/5/release") {
        return json({ released_count: 1, replayed: false });
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

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the two shortage kinds", () => {
  it("gives SHORT and UNIDENTIFIED different badges, colours and words", async () => {
    stubApi();
    renderScreen();

    const shortBadge = await screen.findByText("short");
    const unidentifiedBadge = await screen.findByText("needs identification");

    expect(shortBadge.className).toContain("badge-bad");
    expect(unidentifiedBadge.className).toContain("badge-warn");
    expect(shortBadge.className).not.toBe(unidentifiedBadge.className);
  });

  it("states the shortfall for SHORT but never invents one for UNIDENTIFIED", async () => {
    stubApi();
    renderScreen();

    await screen.findByText("short");
    // The known deficit is stated…
    expect(screen.getByText(/short 15/)).toBeTruthy();
    // …and the unidentified line offers no quantity of any kind — `null`
    // fields must not render as "0 short", which would read as buildable.
    expect(screen.queryByText(/0 free/)).toBeNull();
    expect(screen.queryByText(/short 0/)).toBeNull();
  });

  it("marks the whole build not buildable when anything is unidentified, even satisfied lines present", async () => {
    stubApi({ lines: [SATISFIED_LINE, UNIDENTIFIED_LINE], isBuildable: false });
    renderScreen();

    expect(await screen.findByText("Not buildable")).toBeTruthy();
    expect(
      screen.getByText(/have no part identified at all, so they cannot even be checked/),
    ).toBeTruthy();
  });

  it("says buildable only when the server does, distinct from a merely-short report", async () => {
    stubApi({ lines: [SATISFIED_LINE], isBuildable: true });
    renderScreen();

    expect(await screen.findByText("Buildable")).toBeTruthy();
    expect(screen.queryByText("Not buildable")).toBeNull();
  });

  it("offers no 'reserve stock' action on an unidentified line — there is no part to reserve", async () => {
    stubApi();
    renderScreen();

    await screen.findByText("needs identification");
    expect(screen.queryAllByRole("button", { name: "Reserve stock" })).toHaveLength(1);
  });
});

describe("a hold its lot can no longer fill", () => {
  /**
   * Review finding: the backend used to credit a hold whose bin had been
   * recounted to zero, so a build reported `satisfied`/buildable off stock that
   * was not there. With that fixed the report says "100 required, 100 held,
   * short 100", which reads as a contradiction unless the screen says *why* —
   * hence `undeliverable_milli`, and hence this test.
   */
  const STRANDED_LINE = {
    ...SHORT_LINE,
    required_milli: 100_000,
    allocated_milli: 100_000,
    undeliverable_milli: 100_000,
    available_milli: 0,
    shortfall_milli: 100_000,
  };

  it("says the hold cannot be filled instead of leaving the numbers contradicting", async () => {
    stubApi({ lines: [STRANDED_LINE], isBuildable: false });
    renderScreen();

    expect(await screen.findByText(/can no longer be filled from its lot/)).toBeTruthy();
  });

  it("still offers the whole requirement to re-reserve, not zero", async () => {
    stubApi({ lines: [STRANDED_LINE], isBuildable: false });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Reserve stock" }));
    await screen.findByText(/free ·/);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "900" } });
    fireEvent.click(screen.getByRole("button", { name: /Reserve/ }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/builds/5/allocate")).toBe(true),
    );
    // required (100) - deliverable held (0). Netting against the raw hold would
    // default this to zero on the line that needs the most attention.
    expect(calls.find((call) => call.url === "/api/builds/5/allocate")?.body["qty_milli"]).toBe(
      100_000,
    );
  });

  it("says nothing about deliverability when every hold is real", async () => {
    stubApi({ lines: [SATISFIED_LINE], isBuildable: true });
    renderScreen();

    await screen.findByText("satisfied");
    expect(screen.queryByText(/can no longer be filled/)).toBeNull();
  });
});

describe("reserving stock against a short line", () => {
  it("defaults the quantity to the outstanding shortfall, not the whole requirement", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Reserve stock" }));
    await screen.findByText(/free ·/);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "900" } });
    fireEvent.click(screen.getByRole("button", { name: /Reserve/ }));

    await waitFor(() => expect(calls.some((call) => call.url === "/api/builds/5/allocate")).toBe(true));
    const allocate = calls.find((call) => call.url === "/api/builds/5/allocate");
    // required (30) - allocated (10) = 20 outstanding, in thousandths.
    expect(allocate?.body["qty_milli"]).toBe(20_000);
    expect(allocate?.body["lot_id"]).toBe(900);
    expect(allocate?.body["bom_line_id"]).toBe(10);
  });
});

describe("closing a build", () => {
  it("warns that closing releases every open reservation", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByRole("combobox", { name: /Status/ }), {
      target: { value: "completed" },
    });

    expect(await screen.findByText(/This closes the build/)).toBeTruthy();
  });
});

describe("releasing every hold on a build", () => {
  it("posts a release with no allocation_id, meaning the whole build", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Release all holds" }));

    await waitFor(() => expect(calls.some((call) => call.url === "/api/builds/5/release")).toBe(true));
    const release = calls.find((call) => call.url === "/api/builds/5/release");
    expect(release?.body["allocation_id"]).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Changing the assembly count moves the shortfall — with nothing recomputed
// client-side. Demand is derived server-side (ADR 0004); this only proves
// the screen shows the server's new numbers after the save, not that it
// computed them itself.
// ---------------------------------------------------------------------------

describe("changing the assembly count", () => {
  const PER_ASSEMBLY_MILLI = 1_000;

  /**
   * A stand-in server that actually multiplies `qty_per_assembly_milli` by
   * whatever `assembly_count` a `PATCH` last set — so the shortage report
   * that comes back after the edit is not a second fixture the test hands
   * it, but the same derivation the backend performs. A test that instead
   * stubbed a pre-computed "after" response could pass even if the screen
   * never actually re-read the shortage report at all.
   */
  function stubDerivingApi(): void {
    let assemblyCount = 1;
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
          return json({ ...BUILD, assembly_count: assemblyCount });
        }
        if (url.pathname === "/api/builds/5" && request.method === "PATCH") {
          const body = JSON.parse(raw) as { assembly_count?: number };
          if (body.assembly_count !== undefined) {
            assemblyCount = body.assembly_count;
          }
          return json({ ...BUILD, assembly_count: assemblyCount });
        }
        if (url.pathname === "/api/builds/5/shortages") {
          const required = PER_ASSEMBLY_MILLI * assemblyCount;
          return json({
            build_id: 5,
            assembly_count: assemblyCount,
            is_buildable: required === 0,
            lines: [
              {
                ...SATISFIED_LINE,
                bom_line_id: 20,
                line_no: 1,
                required_milli: required,
                allocated_milli: PER_ASSEMBLY_MILLI,
                shortfall_milli: Math.max(0, required - PER_ASSEMBLY_MILLI),
                kind: required > PER_ASSEMBLY_MILLI ? "short" : "satisfied",
              },
            ],
          });
        }
        throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
      }),
    );
  }

  it("raising it from 1 to 3 triples the required quantity the report shows", async () => {
    stubDerivingApi();
    renderScreen();

    // At count 1, one held unit exactly covers one required unit.
    expect(await screen.findByText(/1 required/)).toBeTruthy();

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Assemblies"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    expect(calls.find((call) => call.method === "PATCH")?.body["assembly_count"]).toBe(3);

    // The same held unit no longer covers triple the demand — the shortfall
    // moved on screen with no second "recompute" request of any kind.
    expect(await screen.findByText(/3 required/)).toBeTruthy();
    expect(await screen.findByText(/short 2/)).toBeTruthy();
    expect(
      calls.filter((call) => call.method === "PATCH" || call.method === "POST"),
    ).toHaveLength(1);
  });

  it("warns, in the user's own words, that raising it only marks parts as needed", async () => {
    stubDerivingApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Assemblies"), { target: { value: "3" } });

    expect(
      await screen.findByText(/marks the extra parts as needed on the shortage report/),
    ).toBeTruthy();
  });

  it("says nothing extra when the count is left unchanged", async () => {
    stubDerivingApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(
      screen.queryByText(/marks the extra parts as needed on the shortage report/),
    ).toBeNull();
  });
});
