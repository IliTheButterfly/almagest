/**
 * Drawing a room and laying containers out in it.
 *
 * Four things are asserted here, and each one is a rule that would be cheap to
 * break and expensive to notice:
 *
 * 1. **A placement is settable with no pointer at all.** This runs on a phone at a
 *    shelf, but drag-only is not acceptable: a box is a `<button>`, it takes the
 *    caret when it appears, and the arrow keys move it by a grid step. jsdom has no
 *    layout, so a drag test here would assert nothing anyway — which is exactly why
 *    the keyboard path is the one that must work.
 * 2. **The tray is real.** A child with no coordinate is listed as "not placed yet",
 *    never drawn at the origin. ADR 0009 refuses a default coordinate because every
 *    pre-existing container would otherwise stand in the same corner of every room
 *    and look authored.
 * 3. **One request for a whole rearrangement.** Moving three things and saving is a
 *    single `PUT …/plan/placements` carrying only what moved. Per-drag writes would
 *    make a rearrangement five requests that can partially fail.
 * 4. **"Back to the tray" is its own field.** `unplace_location_ids`, not a
 *    coordinate of (0, 0), because no coordinate is what nowhere means.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { RoomPlanPanel } from "./RoomPlanPanel";
import type { LocationRead } from "../lib/api/client";

const ROOM = 11;

function room(overrides: Record<string, unknown> = {}): LocationRead {
  return {
    id: ROOM,
    parent_id: null,
    name: "Workshop",
    slot_label: null,
    short_id: null,
    display: null,
    label_path: "Workshop",
    description: null,
    depth: 0,
    id_path: "/11/",
    child_count: 2,
    lot_count: 0,
    qty_milli: 0,
    is_staging: false,
    is_overfull: false,
    is_placeable: false,
    esd_safe: null,
    effective_esd_safe: null,
    child_view: null,
    effective_child_view: "floor_plan",
    glyph: null,
    effective_glyph: null,
    photo: null,
    effective_photo: null,
    container_type_id: null,
    placement: null,
    access_score: 0.5,
    tare_mg: null,
    last_printed_at: null,
    retired_at: null,
    lots: [],
    capacity: {
      model: "none",
      used: 0,
      capacity: null,
      unit: "containers",
      fill_ratio: null,
      is_full: false,
      is_overfull: false,
    },
    ...overrides,
  } as unknown as LocationRead;
}

function child(id: number, name: string): Record<string, unknown> {
  return {
    id,
    parent_id: ROOM,
    name,
    slot_label: null,
    label_path: `Workshop / ${name}`,
    depth: 1,
    id_path: `/11/${id}/`,
    lot_count: 0,
    qty_milli: 0,
    fill_ratio: null,
    is_overfull: false,
    is_staging: false,
    is_placeable: true,
    container_type_id: null,
    effective_child_view: "list",
    effective_glyph: null,
    child_grid_rows: null,
    child_grid_cols: null,
    retired_at: null,
  };
}

function placementRead(
  locationId: number,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    location_id: locationId,
    parent_id: ROOM,
    x_mm: 500,
    y_mm: 500,
    rotation_deg: 0,
    width_mm: 800,
    depth_mm: 600,
    ...overrides,
  };
}

interface Call {
  readonly path: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(plan: Record<string, unknown> = { shapes: [], placements: [] }): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url, "http://localhost");
      const raw = request.method === "GET" || request.method === "DELETE" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ path: url.pathname, method: request.method, body });

      if (url.pathname === `/api/locations/${ROOM}/plan` && request.method === "GET") {
        return json({
          location_id: ROOM,
          shapes: plan["shapes"],
          placements: plan["placements"],
          unplaced_location_ids: [],
          extent: null,
        });
      }
      if (url.pathname === "/api/locations/tree" && request.method === "GET") {
        return json({ nodes: [child(12, "Bench"), child(13, "Steel shelf")] });
      }
      if (url.pathname === `/api/locations/${ROOM}/plan/shapes` && request.method === "PUT") {
        return json({ location_id: ROOM, shapes: [], extent: null, replayed: false });
      }
      if (url.pathname === `/api/locations/${ROOM}/plan/placements` && request.method === "PUT") {
        return json({
          location_id: ROOM,
          placements: [],
          unplaced_location_ids: [],
          extent: null,
          replayed: false,
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderPanel(): { saved: () => number } {
  let saved = 0;
  render(
    <RoomPlanPanel
      location={room()}
      onSaved={() => {
        saved += 1;
      }}
      onDirtyChange={() => {}}
    />,
  );
  return { saved: () => saved };
}

/** The `PUT`s that carry placements, which is what "one request" is counted from. */
function placementPuts(): Call[] {
  return calls.filter((call) => call.method === "PUT" && call.path.endsWith("/plan/placements"));
}

function box(name: string): HTMLElement {
  return screen.getByRole("button", { name: new RegExp(`^${name}, at `) });
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("places a container with the keyboard alone, and says where it went", async () => {
  stubApi();
  renderPanel();

  // Everything starts in the tray, because nothing has a coordinate. Not at the
  // origin, where it would look authored.
  expect(await screen.findByRole("heading", { name: "Not placed yet (2)" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: /^Bench, at / })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "Place Bench" }));

  // The caret follows the box it just created: the button that created it is gone
  // from the tray, and a keyboard user whose focus fell to <body> has lost the
  // thing they placed.
  const bench = box("Bench");
  expect(document.activeElement).toBe(bench);
  expect(bench.getAttribute("aria-label")).toContain("at 2 m across, 2 m down");
  expect(screen.getByRole("heading", { name: "Not placed yet (1)" })).toBeTruthy();

  // One grid step a press, ten with Shift — and the accessible name changes with
  // it, which is the only feedback a non-sighted placement has.
  fireEvent.keyDown(bench, { key: "ArrowRight" });
  fireEvent.keyDown(bench, { key: "ArrowDown" });
  fireEvent.keyDown(bench, { key: "ArrowLeft", shiftKey: true });
  expect(box("Bench").getAttribute("aria-label")).toContain("at 1.1 m across, 2.1 m down");

  fireEvent.click(screen.getByRole("button", { name: "Save the plan" }));

  await waitFor(() => expect(placementPuts()).toHaveLength(1));
  const sent = placementPuts()[0]?.body;
  expect(sent?.["placements"]).toEqual([
    {
      location_id: 12,
      x_mm: 1100,
      y_mm: 2100,
      rotation_deg: 0,
      // Null, not a guess: the container type's own size is the common case.
      width_mm: null,
      depth_mm: null,
    },
  ]);
  expect(sent?.["unplace_location_ids"]).toEqual([]);
  // No drawing changed, so the drawing was not rewritten.
  expect(calls.some((call) => call.path.endsWith("/plan/shapes"))).toBe(false);
});

it("takes a placement from numeric fields too, for the case a drag cannot hit", async () => {
  stubApi();
  renderPanel();
  fireEvent.click(await screen.findByRole("button", { name: "Place Steel shelf" }));

  fireEvent.change(screen.getByLabelText("Across, X (mm)"), { target: { value: "3400" } });
  fireEvent.change(screen.getByLabelText("Down, Y (mm)"), { target: { value: "120" } });
  fireEvent.change(screen.getByLabelText("Width (mm)"), { target: { value: "1800" } });
  fireEvent.click(screen.getByRole("button", { name: "Turn 90°" }));

  fireEvent.click(screen.getByRole("button", { name: "Save the plan" }));
  await waitFor(() => expect(placementPuts()).toHaveLength(1));
  expect(placementPuts()[0]?.body["placements"]).toEqual([
    {
      location_id: 13,
      x_mm: 3400,
      y_mm: 120,
      rotation_deg: 90,
      width_mm: 1800,
      depth_mm: null,
    },
  ]);
});

it("saves a whole rearrangement in ONE request, carrying only what moved", async () => {
  // Both children already stand somewhere, so this is the real case: three moves
  // and a save, not three saves.
  stubApi({ shapes: [], placements: [placementRead(12), placementRead(13, { x_mm: 2000 })] });
  renderPanel();

  const bench = await screen.findByRole("button", { name: /^Bench, at / });
  fireEvent.keyDown(bench, { key: "ArrowRight" });
  fireEvent.keyDown(box("Bench"), { key: "ArrowRight" });
  fireEvent.keyDown(box("Bench"), { key: "ArrowDown" });

  fireEvent.click(screen.getByRole("button", { name: "Save the plan" }));
  await waitFor(() => expect(placementPuts()).toHaveLength(1));

  const sent = placementPuts()[0]?.body["placements"] as Record<string, unknown>[];
  // One request, and the shelf nobody touched is not in it.
  expect(sent).toHaveLength(1);
  expect(sent[0]?.["location_id"]).toBe(12);
  expect(sent[0]?.["x_mm"]).toBe(700);
  expect(sent[0]?.["y_mm"]).toBe(600);
  // The footprint the server gave back is sent back unchanged — it was authored.
  expect(sent[0]?.["width_mm"]).toBe(800);
});

it("returns a container to the tray as its own field, never as a coordinate", async () => {
  stubApi({ shapes: [], placements: [placementRead(12)] });
  renderPanel();

  fireEvent.click(await screen.findByRole("button", { name: /^Bench, at / }));
  fireEvent.click(screen.getByRole("button", { name: "Return Bench to the tray" }));

  expect(screen.queryByRole("button", { name: /^Bench, at / })).toBeNull();
  expect(
    within(screen.getByRole("heading", { name: "Not placed yet (2)" }).parentElement as HTMLElement)
      .getByRole("button", { name: "Place Bench" }),
  ).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Save the plan" }));
  await waitFor(() => expect(placementPuts()).toHaveLength(1));
  expect(placementPuts()[0]?.body["unplace_location_ids"]).toEqual([12]);
  expect(placementPuts()[0]?.body["placements"]).toEqual([]);
});

it("draws the room as a whole drawing, in one write, with nothing placed by it", async () => {
  stubApi();
  renderPanel();

  fireEvent.change(await screen.findByLabelText("Room width (mm)"), { target: { value: "5250" } });
  fireEvent.change(screen.getByLabelText("Room depth (mm)"), { target: { value: "3000" } });
  fireEvent.click(screen.getByRole("button", { name: "Add a rectangular room" }));

  // The drawing is listed in words, not only drawn — the ink is `aria-hidden`.
  expect(screen.getByText(/Outline — the room's own walls · 4 corner\(s\) · closed/)).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "Save the plan" }));
  await waitFor(() =>
    expect(calls.some((call) => call.method === "PUT" && call.path.endsWith("/plan/shapes"))).toBe(
      true,
    ),
  );
  const put = calls.find((call) => call.method === "PUT" && call.path.endsWith("/plan/shapes"));
  const shapes = put?.body["shapes"] as Record<string, unknown>[];
  expect(shapes).toHaveLength(1);
  expect(shapes[0]?.["kind"]).toBe("outline");
  expect(shapes[0]?.["is_closed"]).toBe(true);
  // Snapped to the grid: 5250 is not a multiple of 100 mm.
  expect(shapes[0]?.["points"]).toEqual([
    { x_mm: 0, y_mm: 0 },
    { x_mm: 5300, y_mm: 0 },
    { x_mm: 5300, y_mm: 3000 },
    { x_mm: 0, y_mm: 3000 },
  ]);
  // Drawing walls places nothing, so no placement was written.
  expect(placementPuts()).toHaveLength(0);
});

it("draws a wall from typed corners, and will not send a line of one point", async () => {
  stubApi();
  renderPanel();

  fireEvent.change(await screen.findByLabelText("What to draw next"), { target: { value: "wall" } });
  fireEvent.click(screen.getByRole("button", { name: "Start drawing a wall" }));

  // Saving is refused while a line is open, rather than half-sending it.
  expect(screen.getByRole("button", { name: "Save the plan" })).toHaveProperty("disabled", true);
  expect(screen.getByText(/Finish or throw away the line/)).toBeTruthy();

  fireEvent.change(screen.getByLabelText("X (mm)"), { target: { value: "0" } });
  fireEvent.change(screen.getByLabelText("Y (mm)"), { target: { value: "1000" } });
  fireEvent.click(screen.getByRole("button", { name: "Add this corner" }));
  // One point is not a line: the server refuses it, so Finish does too.
  expect(screen.getByRole("button", { name: "Finish this line" })).toHaveProperty("disabled", true);

  fireEvent.change(screen.getByLabelText("X (mm)"), { target: { value: "2500" } });
  fireEvent.click(screen.getByRole("button", { name: "Add this corner" }));
  fireEvent.click(screen.getByRole("button", { name: "Finish this line" }));

  fireEvent.click(screen.getByRole("button", { name: "Save the plan" }));
  await waitFor(() =>
    expect(calls.some((call) => call.method === "PUT" && call.path.endsWith("/plan/shapes"))).toBe(
      true,
    ),
  );
  const shapes = calls.find((call) => call.path.endsWith("/plan/shapes"))?.body["shapes"] as Record<
    string,
    unknown
  >[];
  expect(shapes[0]?.["kind"]).toBe("wall");
  // A run, not a loop — and its thickness stays unmeasured rather than becoming a
  // number nobody gave.
  expect(shapes[0]?.["is_closed"]).toBe(false);
  expect(shapes[0]?.["thickness_mm"]).toBeNull();
  expect(shapes[0]?.["points"]).toEqual([
    { x_mm: 0, y_mm: 1000 },
    { x_mm: 2500, y_mm: 1000 },
  ]);
});

it("says whether there is unsent work, and does not offer to save when there is none", async () => {
  const dirty: boolean[] = [];
  stubApi({ shapes: [], placements: [placementRead(12)] });
  render(
    <RoomPlanPanel
      location={room()}
      onSaved={() => {}}
      onDirtyChange={(next) => dirty.push(next)}
    />,
  );

  await screen.findByRole("button", { name: /^Bench, at / });
  expect(dirty.at(-1)).toBe(false);
  expect(screen.getByRole("button", { name: "Save the plan" })).toHaveProperty("disabled", true);
  expect(screen.getByText("Saved.")).toBeTruthy();

  fireEvent.keyDown(box("Bench"), { key: "ArrowUp" });
  expect(dirty.at(-1)).toBe(true);
  expect(screen.getByText("Not saved yet.")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Save the plan" })).toHaveProperty("disabled", false);
  // Nothing has been written by the move itself.
  expect(placementPuts()).toHaveLength(0);
});
