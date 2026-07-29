/**
 * Regressions for what review found on the build screen, all four of one shape:
 * **the backend knew and the screen did not say.**
 *
 * * `POST /release` could refuse (a staged row that cannot be given back), and
 *   "Release all holds" chained `.then()` with no `.catch()` — the rejection
 *   became an uncaught error, nothing rendered, and the button just sat there.
 *   Every sibling action in the file already did this correctly; this one alone
 *   did not, which is what made it easy to miss.
 * * `reserved_milli`, `staged_milli`, `consumed_milli` and `needed_milli` were on
 *   the wire and dead in the render: three units held in a drawer, three in a
 *   project box on a shelf and three already soldered into board #1 all showed as
 *   "3 already held". ADR 0004's consequence is explicit that the UI has to keep
 *   them distinguishable, and `needed_milli` — the number the ADR exists to
 *   surface — was never shown at all.
 * * `/stage`, `/unstage` and `/consume-staged` were implemented, tested and in the
 *   generated schema with **nothing in the frontend calling them**, so ADR 0004's
 *   namesake workflow was unreachable from the app it was built for.
 *
 * The assertions are deliberately about words a user reads and requests a server
 * receives, not about component internals: every one of these defects was a gap
 * between a correct backend and a screen, so only the seam is worth testing.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BuildScreen } from "./BuildScreen";

const BUILD = {
  id: 5,
  project_id: 1,
  build_no: 1,
  label: null,
  assembly_count: 3,
  bom_revision: null,
  status: "planned",
  staging_location_id: null,
  started_at: null,
  completed_at: null,
  notes: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

/**
 * Three lines with **identical** `required_milli`, `allocated_milli` and
 * `needed_milli`, differing only in how the allocation splits. Before the fix all
 * three rendered the same sub-line; that identity is the whole point of the
 * fixture, so do not "simplify" the numbers apart.
 */
function line(
  bomLineId: number,
  split: { reserved: number; staged: number; consumed: number },
): Record<string, unknown> {
  return {
    bom_line_id: bomLineId,
    line_no: bomLineId - 9,
    part_id: 42,
    kind: "short",
    required_milli: 9_000,
    allocated_milli: 3_000,
    reserved_milli: split.reserved,
    staged_milli: split.staged,
    consumed_milli: split.consumed,
    needed_milli: 6_000,
    undeliverable_milli: 0,
    available_milli: 50_000,
    shortfall_milli: 0,
    substitute_part_ids: [],
    is_blocking: false,
  };
}

const HELD = line(10, { reserved: 3_000, staged: 0, consumed: 0 });
const SET_ASIDE = line(11, { reserved: 0, staged: 3_000, consumed: 0 });
const BUILT_IN = line(12, { reserved: 0, staged: 0, consumed: 3_000 });

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
  total_qty_milli: 50_000,
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
      qty_milli: 50_000,
      qty_reserved_milli: 0,
      status: "active",
      location_label_path: "Cabinet A / Bin 3",
    },
  ],
};

/** A roster with one `staged` entry — the row the two transitions hang off. */
const ROSTER = {
  build_id: 5,
  assembly_count: 3,
  after_the_fact_milli: 0,
  off_bom_count: 0,
  lines: [
    {
      bom_line_id: 11,
      line_no: 2,
      designators: "R1",
      part_id: 42,
      part_name: "10k 0603 resistor",
      part_mpn: "RC0603FR-0710KL",
      is_dnp: false,
      is_off_bom: false,
      required_milli: 9_000,
      reserved_milli: 0,
      staged_milli: 3_000,
      consumed_milli: 0,
      after_the_fact_milli: 0,
      entries: [
        {
          allocation_id: 77,
          part_id: 42,
          part_name: "10k 0603 resistor",
          part_mpn: "RC0603FR-0710KL",
          lot_id: 901,
          qty_milli: 3_000,
          state: "staged",
          ledger_seq: 12,
          ledger_source: "manual",
          is_after_the_fact: false,
          location_id: 7,
          location_label_path: "PROJECTS / Blinky v2",
          reserved_at: null,
          consumed_at: null,
          note: null,
        },
      ],
    },
  ],
};

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function stubApi(options: { lines?: readonly unknown[]; releaseStatus?: number } = {}): void {
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

      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/builds/5" && request.method === "GET") {
        return json(BUILD);
      }
      if (url.pathname === "/api/builds/5/shortages") {
        return json({
          build_id: 5,
          assembly_count: 3,
          is_buildable: false,
          lines: options.lines ?? [HELD, SET_ASIDE, BUILT_IN],
        });
      }
      if (url.pathname === "/api/builds/5/roster") {
        return json(ROSTER);
      }
      if (url.pathname === "/api/parts/42") {
        return json(PART);
      }
      if (url.pathname === "/api/builds/5/release") {
        const status = options.releaseStatus ?? 200;
        if (status !== 200) {
          return json(
            {
              detail: {
                reason: "is_staged",
                message:
                  "allocation 77 is staged at a project location;" +
                  " un-stage it so the parts go back to the shelf",
              },
            },
            status,
          );
        }
        return json({ released_count: 1, replayed: false });
      }
      if (url.pathname === "/api/builds/5/stage") {
        return json({
          allocation: {
            id: 77,
            build_id: 5,
            bom_line_id: 11,
            part_id: 42,
            lot_id: 901,
            qty_milli: 6_000,
            state: "staged",
            consumed_ledger_seq: null,
            staged_ledger_seq: 12,
            reserved_at: null,
            consumed_at: null,
            note: null,
          },
          source_lot: {
            id: 900,
            part_id: 42,
            location_id: 1,
            qty_milli: 44_000,
            qty_reserved_milli: 0,
            status: "active",
          },
          staging_lot: {
            id: 901,
            part_id: 42,
            location_id: 7,
            qty_milli: 6_000,
            qty_reserved_milli: 0,
            status: "active",
          },
          staging_location_id: 7,
          seqs: [11, 12],
          group_uuid: null,
          replayed: false,
        });
      }
      if (
        url.pathname === "/api/builds/5/unstage" ||
        url.pathname === "/api/builds/5/consume-staged"
      ) {
        return json({ replayed: true }, 200);
      }
      return json({ detail: `unstubbed ${url.pathname}` }, 500);
    }),
  );
}

/**
 * `findAll*`[0] is `HTMLElement | undefined` under `noUncheckedIndexedAccess`,
 * and an `as` cast here would turn "the button never rendered" — the exact
 * regression these tests exist to catch — into a null-dereference two lines
 * later. So the emptiness is asserted, and the failure names itself.
 */
function first(elements: readonly HTMLElement[]): HTMLElement {
  const [element] = elements;
  if (element === undefined) {
    throw new Error("expected at least one matching element");
  }
  return element;
}

function renderScreen(): void {
  render(
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

describe("a refused bulk release", () => {
  it("says so on screen instead of failing silently", async () => {
    stubApi({ releaseStatus: 409 });
    renderScreen();
    const button = await screen.findByRole("button", { name: "Release all holds" });

    fireEvent.click(button);

    // The server's own sentence, not a generic fallback: it names the allocation
    // and says what to do instead, which is the whole value of a 409 body.
    await waitFor(() => {
      expect(screen.getByText(/un-stage it so the parts go back to the shelf/)).toBeTruthy();
    });
  });

  it("keeps the button disabled while it is in flight, so a double tap cannot double-release", async () => {
    stubApi();
    renderScreen();
    const button = await screen.findByRole("button", { name: "Release all holds" });

    fireEvent.click(button);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Releasing…" }).hasAttribute("disabled")).toBe(
        true,
      );
    });
    await waitFor(() => {
      expect(calls.filter((call) => call.url === "/api/builds/5/release")).toHaveLength(1);
    });
  });
});

describe("a shortage line's three quantities", () => {
  it("reads differently for held, set aside and built in", async () => {
    stubApi();
    renderScreen();
    await screen.findAllByText(/9 required/);

    expect(screen.getByText(/3 held in a bin/)).toBeTruthy();
    expect(screen.getByText(/3 set aside for this project/)).toBeTruthy();
    expect(screen.getByText(/3 built in/)).toBeTruthy();

    // The defect, stated as an assertion: identical `allocated_milli` across the
    // three lines must not produce identical text.
    const texts = screen
      .getAllByText(/required/)
      .map((node) => node.parentElement?.textContent ?? "");
    expect(new Set(texts).size).toBe(3);
  });

  it("shows how much is still to get, which no number on screen used to say", async () => {
    stubApi();
    renderScreen();

    // `needed_milli` — required minus what is deliverably accounted for. Not
    // `shortfall_milli` (0 here, because free stock covers it): the two answer
    // different questions and the report carries both.
    await waitFor(() => {
      expect(screen.getAllByText(/6 still to get/)).toHaveLength(3);
    });
  });

  it("prints nothing at all for a line holding nothing, rather than three zeroes", async () => {
    stubApi({
      lines: [
        {
          ...HELD,
          allocated_milli: 0,
          reserved_milli: 0,
          staged_milli: 0,
          consumed_milli: 0,
          needed_milli: 9_000,
        },
      ],
    });
    renderScreen();
    await screen.findAllByText(/9 required/);

    expect(screen.queryByText(/held in a bin/)).toBeNull();
    expect(screen.queryByText(/built in/)).toBeNull();
  });
});

describe("ADR 0004's namesake workflow", () => {
  it("can send parts to the project from a shortage line", async () => {
    stubApi();
    renderScreen();
    const open = first(await screen.findAllByRole("button", { name: "Send to project" }));

    fireEvent.click(open);
    await screen.findByRole("button", { name: /^Send [\d,]/ });
    fireEvent.change(screen.getByLabelText("Take from"), { target: { value: "900" } });
    fireEvent.click(screen.getByRole("button", { name: /^Send [\d,]/ }));

    await waitFor(() => {
      const staged = calls.find((call) => call.url === "/api/builds/5/stage");
      expect(staged).toBeDefined();
      expect(staged?.body.lot_id).toBe(900);
      // Defaulted to what the line still needs, not to the whole requirement.
      expect(staged?.body.qty_milli).toBe(6_000);
      // Omitted destination means the project's floating parts, which is a real
      // choice and not a missing value — so it goes on the wire as null.
      expect(staged?.body.assembly_no).toBeNull();
      expect(typeof staged?.body.client_op_id).toBe("string");
    });
  });

  it("can commit them to one specific assembly", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(first(await screen.findAllByRole("button", { name: "Send to project" })));
    await screen.findByLabelText("Send to");

    fireEvent.change(screen.getByLabelText("Take from"), { target: { value: "900" } });
    fireEvent.change(screen.getByLabelText("Send to"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: /^Send [\d,]/ }));

    await waitFor(() => {
      expect(calls.find((call) => call.url === "/api/builds/5/stage")?.body.assembly_no).toBe(2);
    });
  });

  it("warns that the parts really move, not that they are merely marked", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(first(await screen.findAllByRole("button", { name: "Send to project" })));

    // The ADR rejected the flag-only implementation because a drawer whose count
    // still includes parts that have left is the failure the system exists to
    // prevent. The wording has to match what actually happens.
    expect(await screen.findByText(/This moves the parts for real/)).toBeTruthy();
  });

  it("offers both ways out of staged on the roster row that says where they are", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Roster" }));
    const back = await screen.findByRole("button", { name: "Put back on the shelf" });

    fireEvent.click(back);
    await waitFor(() => {
      const call = calls.find((entry) => entry.url === "/api/builds/5/unstage");
      expect(call?.body.allocation_id).toBe(77);
    });

    fireEvent.click(screen.getByRole("button", { name: "Built in" }));
    await waitFor(() => {
      const call = calls.find((entry) => entry.url === "/api/builds/5/consume-staged");
      expect(call?.body.allocation_id).toBe(77);
      // Blank quantity means all of it — the server spells that `null`.
      expect(call?.body.qty_milli).toBeNull();
    });
  });

  it("passes a partial build through as a partial build", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Roster" }));
    const qty = await screen.findByLabelText("How many were built in");

    fireEvent.change(qty, { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Built in" }));

    await waitFor(() => {
      expect(calls.find((entry) => entry.url === "/api/builds/5/consume-staged")?.body.qty_milli).toBe(
        2_000,
      );
    });
  });
});
