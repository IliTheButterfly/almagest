/**
 * The type canvas editor, against a stubbed `fetch`.
 *
 * The behaviour under test is what ADR 0002 and the module docstring on
 * `app.services.layout_authoring` promise for a **type**'s own canvas,
 * which is deliberately simpler than an instance's: there is no change
 * guard here at all (a type has no children to hold content), and editing
 * a seed clones it rather than mutating the shared original.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ContainerTypeScreen } from "./ContainerTypeScreen";

function containerType(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 7,
    slug: "raaco-15-drawer",
    display_name: "Raaco 15-drawer",
    description: "A 15-drawer organiser.",
    child_layout: "grid",
    grid_rows: 1,
    grid_cols: 2,
    grid_pitch_mm: null,
    grid_height_unit_mm: null,
    footprint_cols: null,
    footprint_rows: null,
    footprint_height_u: null,
    slot_label_scheme: "row_alpha_col_num",
    slot_label_params: null,
    materialize_slots: false,
    capacity_model: "slots",
    capacity_slots: 2,
    max_parts_per_slot: null,
    inner_length_mm: null,
    inner_width_mm: null,
    inner_height_mm: null,
    default_fill_factor: 0.85,
    full_threshold: 0.9,
    esd_safe: null,
    is_placeable: true,
    max_item_dimension_mm: null,
    allowed_part_kinds: null,
    front_width_mm: null,
    front_height_mm: null,
    is_seed: false,
    ...overrides,
  };
}

function slotTemplate(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    container_type_id: 7,
    materialize_slots: false,
    grid_rows: 1,
    grid_cols: 2,
    slot_label_scheme: "row_alpha_col_num",
    slot_label_params: null,
    slots: [
      {
        row_idx: 0,
        col_idx: 0,
        row_span: 1,
        col_span: 1,
        slot_label: "A1",
        size_class: null,
        inner_volume_mm3: null,
        sort_order: 0,
      },
      {
        row_idx: 0,
        col_idx: 1,
        row_span: 1,
        col_span: 1,
        slot_label: "A2",
        size_class: null,
        inner_volume_mm3: null,
        sort_order: 10,
      },
    ],
    ...overrides,
  };
}

interface Call {
  readonly url: string;
  readonly method: string;
  readonly body: Record<string, unknown>;
}

const calls: Call[] = [];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function clickCell(label: string, options: { shiftKey?: boolean } = {}): void {
  const span = screen.getByText(label, { selector: ".cell-slot" });
  const button = span.closest("button");
  if (button === null) {
    throw new Error(`no button ancestor for cell ${label}`);
  }
  fireEvent.click(button, options);
}

function stubApi(
  options: {
    isSeed?: boolean;
    putResponse?: (body: Record<string, unknown>) => unknown;
  } = {},
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ url: url.pathname, method: request.method, body });

      if (url.pathname === "/api/container-types/7" && request.method === "GET") {
        return json(containerType({ is_seed: options.isSeed ?? false }));
      }
      if (url.pathname === "/api/container-types/7/slot-template" && request.method === "GET") {
        return json(slotTemplate());
      }
      if (url.pathname === "/api/container-types/7/slot-template" && request.method === "PUT") {
        const written = options.putResponse?.(body) ?? {
          template: slotTemplate({ slots: body["slots"] }),
          cloned: false,
          container_type_id: 7,
          replayed: false,
        };
        return json(written);
      }
      if (url.pathname === "/api/container-types/99" && request.method === "GET") {
        return json(containerType({ id: 99, slug: "raaco-15-drawer-copy", is_seed: false }));
      }
      if (url.pathname === "/api/container-types/99/slot-template" && request.method === "GET") {
        return json(slotTemplate({ container_type_id: 99 }));
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/container-types/7"]}>
      <Routes>
        <Route path="/container-types/:containerTypeId" element={<ContainerTypeScreen />} />
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

describe("the type canvas", () => {
  it("renders every slot the effective layout reports", async () => {
    stubApi();
    renderScreen();

    expect(await screen.findByText("A1", { selector: ".cell-slot" })).toBeTruthy();
    expect(screen.getByText("A2", { selector: ".cell-slot" })).toBeTruthy();
  });

  it("merges two selected cells into one region and saves it with a blank label, letting the server generate one", async () => {
    stubApi();
    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });

    clickCell("A1");
    clickCell("A2", { shiftKey: true });
    fireEvent.click(await screen.findByRole("button", { name: "Merge" }));
    fireEvent.click(screen.getByRole("button", { name: /Save layout/ }));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "PUT" && call.url.endsWith("/slot-template"))).toBe(
        true,
      ),
    );
    const put = calls.find((call) => call.method === "PUT" && call.url.endsWith("/slot-template"));
    const slots = put?.body["slots"] as { row_span: number; col_span: number; slot_label?: string }[];
    expect(slots).toHaveLength(1);
    expect(slots[0]).toMatchObject({ row_idx: 0, col_idx: 0, row_span: 1, col_span: 2 });
    // Left blank on purpose — a type's canvas has a generator to fall back
    // on, so the request omits the label rather than inventing one client-side.
    expect(slots[0]?.slot_label).toBeUndefined();

    expect(await screen.findByText("Saved.")).toBeTruthy();
  });

  it("splits a merged region back into its base cells with distinct suggested labels", async () => {
    stubApi({
      putResponse: () => ({
        template: slotTemplate(),
        cloned: false,
        container_type_id: 7,
        replayed: false,
      }),
    });
    // Start from an already-merged template so there is something to split.
    // Re-stub the GET to return a 1x2 merged slot instead of two 1x1s.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        const url = new URL(request.url);
        const raw = request.method === "GET" ? "" : await request.text();
        const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
        calls.push({ url: url.pathname, method: request.method, body });
        if (url.pathname === "/api/container-types/7" && request.method === "GET") {
          return json(containerType());
        }
        if (url.pathname === "/api/container-types/7/slot-template" && request.method === "GET") {
          return json(
            slotTemplate({
              slots: [
                {
                  row_idx: 0,
                  col_idx: 0,
                  row_span: 1,
                  col_span: 2,
                  slot_label: "A1",
                  size_class: null,
                  inner_volume_mm3: null,
                  sort_order: 0,
                },
              ],
            }),
          );
        }
        if (url.pathname === "/api/container-types/7/slot-template" && request.method === "PUT") {
          return json({ template: slotTemplate(), cloned: false, container_type_id: 7, replayed: false });
        }
        throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
      }),
    );

    renderScreen();
    await screen.findAllByText("A1", { selector: ".cell-slot" });

    clickCell("A1");
    fireEvent.click(await screen.findByRole("button", { name: /Split into 2 cells/ }));
    fireEvent.click(screen.getByRole("button", { name: /Save layout/ }));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "PUT" && call.url.endsWith("/slot-template"))).toBe(
        true,
      ),
    );
    const put = calls.find((call) => call.method === "PUT" && call.url.endsWith("/slot-template"));
    const slots = put?.body["slots"] as { row_idx: number; col_idx: number }[];
    expect(slots).toHaveLength(2);
    expect(slots.map((slot) => slot.col_idx).sort()).toEqual([0, 1]);
  });

  it("never calls any /locations endpoint — editing a type touches no instance", async () => {
    stubApi();
    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });

    clickCell("A1");
    fireEvent.click(screen.getByRole("button", { name: /Remove this slot/ }));
    fireEvent.click(screen.getByRole("button", { name: /Save layout/ }));

    await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
    expect(calls.some((call) => call.url.includes("/locations"))).toBe(false);
  });

  it("a seed type clones on save and the screen follows the clone's id", async () => {
    stubApi({
      isSeed: true,
      putResponse: () => ({
        template: slotTemplate({ container_type_id: 99 }),
        cloned: true,
        container_type_id: 99,
        replayed: false,
      }),
    });
    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });
    expect(screen.getByText("seed")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Save layout/ }));

    expect(await screen.findByText(/Saved as a new type/)).toBeTruthy();
    // Following the clone means the screen re-fetches the new id.
    await waitFor(() => expect(calls.some((call) => call.url === "/api/container-types/99")).toBe(true));
  });
});
