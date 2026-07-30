/**
 * Draining the inbox, against a stubbed `fetch`.
 *
 * The behaviours here are the ones that decide whether the screen is trusted:
 *
 * - it finds staging by the flag, not by a name, and leaves a **project's** box
 *   alone — same flag, opposite meaning (ADR 0004);
 * - many lots, one destination, one pass — that is the whole point, since sorting
 *   at a bench happens by drawer;
 * - **one key per lot**, because a single key across two moves would make the
 *   server replay the first lot's answer for the second;
 * - a lot that fails fails alone, and is named. Losing four successful moves to
 *   report the fifth's failure is how this screen would stop being used.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StagingScreen } from "./StagingScreen";

function node(id: number, name: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    parent_id: null,
    name,
    slot_label: null,
    label_path: name,
    id_path: `/${id}/`,
    depth: 0,
    lot_count: 0,
    qty_milli: 0,
    fill_ratio: null,
    is_overfull: false,
    is_staging: false,
    is_placeable: true,
    container_type_id: null,
    child_grid_rows: null,
    child_grid_cols: null,
    effective_child_view: "list",
    effective_glyph: null,
    ...extra,
  };
}

const NODES = [
  node(1, "Workshop"),
  node(2, "Cabinet A"),
  // The inbox: staging and still a legitimate home for auto-assignment.
  node(3, "INBOX", { is_staging: true, lot_count: 2 }),
  // A project's box: same flag, `is_placeable: false`. Must not be drained here.
  node(4, "Blinky parts", { is_staging: true, is_placeable: false, lot_count: 1 }),
];

function lot(id: number, partId: number, qtyMilli: number) {
  return {
    id,
    part_id: partId,
    location_id: 3,
    location_label_path: "INBOX",
    qty_milli: qtyMilli,
    qty_reserved_milli: 0,
    status: "active",
    packaging_id: null,
    batch_code: null,
    date_code: null,
    serial: null,
    unit_cost_micro: null,
    currency: null,
  };
}

interface Call {
  readonly url: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

/** `failing` is the lot id whose move is refused. */
function stubApi(options: { failing?: number } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      calls.push({
        url: url.pathname,
        body: raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>),
      });
      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (url.pathname === "/api/locations/tree") {
        return json({ nodes: NODES });
      }
      if (url.pathname === "/api/locations/3") {
        return json({
          id: 3,
          name: "INBOX",
          label_path: "INBOX",
          parent_id: null,
          slot_label: null,
          short_id: null,
          description: null,
          depth: 0,
          child_count: 0,
          is_staging: true,
          is_overfull: false,
          is_placeable: true,
          effective_esd_safe: null,
          child_view: null,
          effective_child_view: "list",
          glyph: null,
          effective_glyph: null,
          photo: null,
          effective_photo: null,
          container_type_id: null,
          id_path: "/3/",
          access_score: 0,
          display: "BIN",
          last_printed_at: null,
          tare_mg: null,
          lots: [lot(51, 7, 4000), lot(52, 8, 1000)],
          capacity: {
            model: "none",
            used: 0,
            capacity: null,
            unit: "slots",
            fill_ratio: null,
            is_overfull: false,
          },
        });
      }
      if (url.pathname === "/api/parts/7") {
        return json({ id: 7, name: "10k 0805", mpn: "RC0805", lots: [] });
      }
      if (url.pathname === "/api/parts/8") {
        return json({ id: 8, name: "100nF 0603", mpn: "CC0603", lots: [] });
      }
      const move = /^\/api\/stock\/lots\/(\d+)\/move$/.exec(url.pathname);
      if (move !== null) {
        const lotId = Number(move[1]);
        if (options.failing === lotId) {
          return json({ detail: "lot is empty" }, 409);
        }
        return json({
          lot: { ...lot(lotId, 7, 4000), location_id: 2 },
          entry: null,
          replayed: false,
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter>
      <StagingScreen />
    </MemoryRouter>,
  );
}

/** Narrows the picker to one row, then takes it — there is a "Send here" per row. */
async function pickCabinetA(): Promise<void> {
  fireEvent.change(await screen.findByLabelText(/Find a container/i), {
    target: { value: "cabinet a" },
  });
  fireEvent.click(await screen.findByRole("button", { name: "Send here" }));
}

function moveCalls(): Call[] {
  return calls.filter((call) => call.url.endsWith("/move"));
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the worklist", () => {
  it("lists what is in the inbox by part name, not by row id", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText("10k 0805")).toBeTruthy();
    expect(screen.getByText("100nF 0603")).toBeTruthy();
  });

  it("leaves a project's staging box alone, and says it is doing so", async () => {
    stubApi();
    renderScreen();

    // Same `is_staging` flag, opposite meaning: those parts are set aside on
    // purpose, so its contents are never fetched, let alone offered up.
    expect(await screen.findByText(/1 project parts box\(es\)/)).toBeTruthy();
    await waitFor(() => expect(calls.some((call) => call.url === "/api/locations/4")).toBe(false));
  });
});

describe("moving several lots in one pass", () => {
  it("sends each lot its own move with its own key", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Select all" }));
    await pickCabinetA();
    fireEvent.click(screen.getByRole("button", { name: /Move 2 lot\(s\) to Cabinet A/ }));

    await waitFor(() => expect(moveCalls().length).toBe(2));
    const keys = moveCalls().map((call) => call.body["client_op_id"]);
    expect(new Set(keys).size).toBe(2);
    expect(moveCalls().every((call) => call.body["to_location_id"] === 2)).toBe(true);
    expect(await screen.findByText(/Moved 2 lot\(s\) to Cabinet A/)).toBeTruthy();
  });

  it("will not move anything until a destination is chosen", async () => {
    stubApi();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Select all" }));

    expect(screen.getByRole("button", { name: "Choose where they go" })).toHaveProperty(
      "disabled",
      true,
    );
    expect(moveCalls()).toHaveLength(0);
  });

  it("fails one lot without losing the others", async () => {
    stubApi({ failing: 52 });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: "Select all" }));
    await pickCabinetA();
    fireEvent.click(screen.getByRole("button", { name: /Move 2 lot\(s\)/ }));

    expect(await screen.findByText(/Moved 1 lot\(s\) to Cabinet A/)).toBeTruthy();
    expect(screen.getByText(/lot 52/)).toBeTruthy();
    expect(moveCalls().length).toBe(2);
  });
});
