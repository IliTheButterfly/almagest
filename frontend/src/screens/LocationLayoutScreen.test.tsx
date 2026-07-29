/**
 * The instance layout editor, against a stubbed `fetch`.
 *
 * The three behaviours under test are the ones ADR 0002 and
 * `app.services.layout_authoring` name explicitly: *safe*, *guarded* and
 * *refused* must read as three different things on screen, not shades of
 * the same "it didn't save" message — and loading a type's current layout
 * into the draft must never itself reach `reapply-layout`, because pushing
 * a type's change into one instance is a separate, explicit step through
 * the same guard as any hand-drawn edit.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LocationLayoutScreen } from "./LocationLayoutScreen";

const LOCATION = {
  id: 11,
  name: "Drawer bank",
  label_path: "Workshop / Cabinet A / Drawer bank",
  container_type_id: 7,
  child_count: 2,
  // ADR 0006: nothing pinned here, so this instance draws whatever its type
  // draws — which the API has already resolved into `effective_child_view`.
  child_view: null,
  effective_child_view: "cabinet_face",
};

function slotState(overrides: Record<string, unknown>): Record<string, unknown> {
  return {
    location_id: 100,
    slot_label: "A1",
    row_idx: 0,
    col_idx: 0,
    row_span: 1,
    col_span: 1,
    size_class: null,
    inner_volume_mm3: null,
    sort_order: 0,
    short_id: null,
    has_tag: false,
    lot_count: 0,
    qty_milli: 0,
    ...overrides,
  };
}

/** A1 is empty; A2 holds a lot and is the guarded case's target. */
function layoutRead(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    location_id: 11,
    container_type_id: 7,
    grid_rows: 1,
    grid_cols: 2,
    slots: [
      slotState({ location_id: 100, slot_label: "A1", col_idx: 0 }),
      slotState({ location_id: 101, slot_label: "A2", col_idx: 1, lot_count: 2, qty_milli: 5000 }),
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

function errorJson(detail: Record<string, unknown>, status: number): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function clickCell(label: string, options: { shiftKey?: boolean } = {}): void {
  const span = screen.getByText(label, { selector: ".cell-slot" });
  const button = span.closest("button");
  if (button === null) {
    throw new Error(`no button ancestor for cell ${label}`);
  }
  fireEvent.click(button, options);
}

function stubApi(options: {
  reapplyResponse?: () => Response;
  typeSlotTemplate?: Record<string, unknown>;
} = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ url: url.pathname, method: request.method, body });

      if (url.pathname === "/api/locations/11" && request.method === "GET") {
        return json(LOCATION);
      }
      if (url.pathname === "/api/locations/11/layout" && request.method === "GET") {
        return json(layoutRead());
      }
      if (url.pathname === "/api/container-types/7" && request.method === "GET") {
        return json({
          id: 7,
          display_name: "Raaco 15-drawer",
          is_seed: false,
          child_view: null,
          effective_child_view: "cabinet_face",
        });
      }
      if (url.pathname === "/api/locations/11/child-view" && request.method === "PUT") {
        const chosen = body["child_view"];
        return json({
          location_id: 11,
          child_view: chosen,
          effective_child_view: chosen ?? "cabinet_face",
          replayed: false,
        });
      }
      if (url.pathname === "/api/container-types/7/slot-template" && request.method === "GET") {
        return json(
          options.typeSlotTemplate ?? {
            container_type_id: 7,
            materialize_slots: false,
            grid_rows: 1,
            grid_cols: 3,
            slot_label_scheme: "row_alpha_col_num",
            slot_label_params: null,
            slots: [
              { row_idx: 0, col_idx: 0, row_span: 1, col_span: 1, slot_label: "A1", size_class: null, inner_volume_mm3: null, sort_order: 0 },
              { row_idx: 0, col_idx: 1, row_span: 1, col_span: 1, slot_label: "A2", size_class: null, inner_volume_mm3: null, sort_order: 10 },
              { row_idx: 0, col_idx: 2, row_span: 1, col_span: 1, slot_label: "A3", size_class: null, inner_volume_mm3: null, sort_order: 20 },
            ],
          },
        );
      }
      if (url.pathname === "/api/locations/11/reapply-layout" && request.method === "POST") {
        return options.reapplyResponse?.() ?? json({ created: 0, updated: 1, deleted: 0, layout: layoutRead(), replayed: false });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/locations/11/layout"]}>
      <Routes>
        <Route path="/locations/:locationId/layout" element={<LocationLayoutScreen />} />
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

describe("loading the current layout", () => {
  it("shows each slot's physical content, so the guard's stakes are visible before any edit", async () => {
    stubApi();
    renderScreen();

    await screen.findByText("A1", { selector: ".cell-slot" });
    expect(screen.getByText("2 lot(s)")).toBeTruthy();
  });
});

describe("a safe change", () => {
  it("relabels in place and reports the save distinctly from a guarded or refused one", async () => {
    stubApi({
      reapplyResponse: () =>
        json({ created: 0, updated: 1, deleted: 0, layout: layoutRead(), replayed: false }),
    });
    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });

    clickCell("A1");
    fireEvent.change(await screen.findByLabelText(/Label \(required\)/), {
      target: { value: "shelf-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST" && call.url.endsWith("/reapply-layout"))).toBe(
        true,
      ),
    );
    const post = calls.find((call) => call.method === "POST" && call.url.endsWith("/reapply-layout"));
    expect(post?.body["slots"]).toEqual(
      expect.arrayContaining([expect.objectContaining({ slot_label: "shelf-1" })]),
    );

    const saved = await screen.findByText(/created, 1 updated, 0 removed/);
    expect(saved.closest(".notice")?.className).toContain("notice-ok");
  });
});

describe("a guarded change", () => {
  it("previews the block before saving, then shows the server's authoritative list as a warning, not an error", async () => {
    stubApi({
      reapplyResponse: () =>
        errorJson(
          {
            reason: "slots_hold_content",
            message: "some slots hold content",
            affected_slots: [{ location_id: 101, slot_label: "A2", reasons: ["has_stock"] }],
          },
          409,
        ),
    });
    renderScreen();
    await screen.findByText("A2", { selector: ".cell-slot" });

    // Select A2 (which holds a lot) and remove it.
    clickCell("A2");
    fireEvent.click(await screen.findByRole("button", { name: "Remove this slot" }));

    // The client already knows this slot holds stock, before Save is pressed.
    expect(await screen.findByText(/blocked/)).toBeTruthy();
    expect(screen.getByText(/holds stock/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    const notice = await screen.findByText(/Blocked — some slots still hold content/);
    expect(notice.closest(".notice")?.className).toContain("notice-warn");
    expect(screen.getByRole("link", { name: "A2" }).getAttribute("href")).toBe("/locations/101");
  });
});

describe("a refused change", () => {
  it("disables Save when reusing an existing slot's label would reinterpret its identity", async () => {
    stubApi();
    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });

    // Rename A1 to "A2" — the label A2's own region already owns.
    clickCell("A1");
    fireEvent.change(await screen.findByLabelText(/Label \(required\)/), {
      target: { value: "A2" },
    });

    expect(await screen.findByText(/would reinterpret/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });
});

describe("loading a type's current layout into the draft", () => {
  it("never calls reapply-layout by itself — it only replaces the draft, which must still be saved", async () => {
    stubApi();
    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });

    fireEvent.click(await screen.findByRole("button", { name: "Load the type's current layout" }));

    // The type's canvas has three cells (A1, A2, A3); this instance had two.
    await screen.findByText("A3", { selector: ".cell-slot" });
    expect(calls.some((call) => call.url.endsWith("/reapply-layout"))).toBe(false);

    // Saving afterward is the separate, explicit step that actually reaches
    // the instance through the guard.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.some((call) => call.method === "POST" && call.url.endsWith("/reapply-layout"))).toBe(
        true,
      ),
    );
  });

  it("offers no such button for a container that was never stamped from a type", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        const url = new URL(request.url);
        if (url.pathname === "/api/locations/11" && request.method === "GET") {
          return json({ ...LOCATION, container_type_id: null });
        }
        if (url.pathname === "/api/locations/11/layout" && request.method === "GET") {
          return json(layoutRead({ container_type_id: null }));
        }
        throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
      }),
    );
    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });
    expect(screen.queryByRole("button", { name: /Load the type's current layout/ })).toBeNull();
  });
});

/**
 * ADR 0006's instance half: the type says how every container of its kind draws,
 * and one particular container can disagree.
 */
describe("how this one container is drawn", () => {
  it("starts on the type's answer and says nobody has overridden it", async () => {
    stubApi();
    renderScreen();

    const picker = (await screen.findByLabelText("Picture")) as HTMLSelectElement;
    expect(picker.value).toBe("");
    expect(screen.getByText(/Not overridden here/)).toBeTruthy();
    expect(screen.getByText(/currently drawn as: cabinet face/i)).toBeTruthy();
  });

  it("pins one, and reports back what it is now drawn as", async () => {
    stubApi();
    renderScreen();
    fireEvent.change(await screen.findByLabelText("Picture"), {
      target: { value: "grid_cells" },
    });

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/locations/11/child-view")).toBe(true),
    );
    const put = calls.find((call) => call.url === "/api/locations/11/child-view");
    expect(put?.method).toBe("PUT");
    expect(put?.body["child_view"]).toBe("grid_cells");
    expect(await screen.findByText(/Overridden for this container only/)).toBeTruthy();
    expect(screen.getByText(/currently drawn as: grid/i)).toBeTruthy();
  });

  it("never touches the layout — a picture cannot swallow a neighbour's stock", async () => {
    // Which is why this saves on its own rather than behind the Save button that
    // the change guard protects: routing it through there would imply a risk it
    // does not carry.
    stubApi();
    renderScreen();
    fireEvent.change(await screen.findByLabelText("Picture"), { target: { value: "list" } });

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/locations/11/child-view")).toBe(true),
    );
    expect(calls.some((call) => call.url.includes("reapply-layout"))).toBe(false);
  });

  it("sends an explicit null to hand the drawing back to the type", async () => {
    stubApi();
    renderScreen();
    const picker = await screen.findByLabelText("Picture");
    fireEvent.change(picker, { target: { value: "list" } });
    await waitFor(() => expect(screen.getByText(/Overridden for this container only/)).toBeTruthy());

    fireEvent.change(picker, { target: { value: "" } });
    await waitFor(() =>
      expect(
        calls.filter((call) => call.url === "/api/locations/11/child-view"),
      ).toHaveLength(2),
    );
    const last = calls.filter((call) => call.url === "/api/locations/11/child-view").at(-1);
    expect(last?.body).toHaveProperty("child_view", null);
    expect(await screen.findByText(/Not overridden here/)).toBeTruthy();
  });
});
