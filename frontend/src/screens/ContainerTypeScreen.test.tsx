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
    // Unpinned, so the API reports what the geometry derives to: a grid with no
    // declared pitch is a face of drawer fronts, not a tray seen from above.
    child_view: null,
    effective_child_view: "cabinet_face",
    glyph: null,
    photo: null,
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
    /** The id a PATCH comes back as, when the target is a seed and was cloned. */
    patchClonesTo?: number;
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
      if (url.pathname === "/api/container-types/7" && request.method === "PATCH") {
        const clonedTo = options.patchClonesTo;
        return json({
          container_type: containerType({
            ...body,
            child_view: body["child_view"],
            ...(clonedTo === undefined ? {} : { id: clonedTo, is_seed: false }),
          }),
          cloned: clonedTo !== undefined,
          replayed: false,
        });
      }
      if (url.pathname === "/api/container-types/7/clone" && request.method === "POST") {
        return json({
          container_type: containerType({ id: 99, slug: "raaco-15-drawer-copy", is_seed: false }),
          replayed: false,
        });
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

  /**
   * Regression, from adversarial review: **the photo was the one edit on this
   * screen that did not follow the clone.**
   *
   * Every other write here — `PATCH`, `PUT .../slot-template` — reports
   * `cloned` and this screen navigates to the id it comes back with, because
   * staying on the seed's id means the *next* save clones the seed a second
   * time. The photo handler passed `type.id` through and called `onReload()`
   * unconditionally, which reloaded the seed and showed no picture (the link had
   * landed on a copy), with nothing on screen to explain either.
   *
   * The server-side half — that the seed row is no longer dressed at all — is
   * `backend/tests/integration/test_container_authoring_findings.py`. This is the
   * half that makes the clone visible to the person who made it.
   */
  it("a seed type clones when given a photo, and the screen follows that copy too", async () => {
    // A bespoke stub rather than `stubApi`: `uploadDocument` calls `fetch` with a
    // (url, init) pair and a binary body, not the JSON `Request` the shared stub
    // parses.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: Request | string, init?: RequestInit) => {
        const request = typeof input === "string" ? new Request(input, init) : input;
        const url = new URL(request.url);
        calls.push({ url: url.pathname, method: request.method, body: {} });

        if (url.pathname === "/api/container-types/7" && request.method === "GET") {
          return json(containerType({ is_seed: true }));
        }
        if (url.pathname === "/api/container-types/7/slot-template" && request.method === "GET") {
          return json(slotTemplate());
        }
        if (url.pathname === "/api/container-types/99" && request.method === "GET") {
          return json(containerType({ id: 99, slug: "raaco-15-drawer-copy", is_seed: false }));
        }
        if (url.pathname === "/api/container-types/99/slot-template" && request.method === "GET") {
          return json(slotTemplate({ container_type_id: 99 }));
        }
        if (url.pathname === "/api/documents" && request.method === "POST") {
          // What the route now answers for a seed: the link landed elsewhere.
          expect(url.searchParams.get("container_type_id")).toBe("7");
          expect(url.searchParams.get("role")).toBe("photo");
          return json({
            document: {
              id: 1,
              sha256: "a".repeat(64),
              kind: "photo",
              media_type: "image/png",
              byte_size: 9,
              page_count: null,
              source_url: null,
              original_filename: null,
              created_at: "2026-07-29T00:00:00Z",
              url: `/api/documents/${"a".repeat(64)}`,
            },
            created: true,
            deduplicated: false,
            link: null,
            container_type_id: 99,
            cloned_container_type: true,
          });
        }
        throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
      }),
    );

    renderScreen();
    await screen.findByText("A1", { selector: ".cell-slot" });

    const picker = screen.getByLabelText("Add a photo");
    const file = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "bench.png", {
      type: "image/png",
    });
    fireEvent.change(picker, { target: { files: [file] } });

    expect(await screen.findByText(/Saved as a new type/)).toBeTruthy();
    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/container-types/99")).toBe(true),
    );
  });
});

/**
 * ADR 0006's type half: the drawing is a property of the type, so it is edited
 * here and every container stamped from the type inherits it.
 */
describe("how the type draws its children", () => {
  it("says what it currently draws as, and that nobody chose it", async () => {
    stubApi();
    renderScreen();
    // `child_view` is null and the geometry is an unmeasured grid, so the API
    // derived a cabinet face — and the screen says the derivation is why.
    expect(await screen.findByText(/Cabinet face/)).toBeTruthy();
    expect(screen.getByText(/worked out from the layout/)).toBeTruthy();
  });

  it("pins a view kind, sending it as a plain field on the type", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));

    fireEvent.change(screen.getByLabelText("Picture used for its contents"), {
      target: { value: "grid_cells" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH");
    expect(patch?.body["child_view"]).toBe("grid_cells");
    // The canvas is untouched: what the picture looks like and where the slots
    // are are two different questions.
    expect(patch?.body["slots"]).toBeUndefined();
  });

  it("sends an explicit null to stop pinning, rather than omitting the field", async () => {
    // Omitting it would mean "leave it alone", so "go back to being worked out"
    // would be unsayable — `_apply` keys off `model_fields_set` on purpose.
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH");
    expect(patch?.body).toHaveProperty("child_view", null);
  });
});

/**
 * The half this batch added: everything about a type that is *not* its slot
 * canvas, edited through the same form the create screen uses.
 */
describe("editing what the type is", () => {
  it("asks ADR 0002's two questions as two separate questions", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));

    // The two legends, and the fields under each labelled in those terms. A user
    // who cannot tell "what do I offer" from "what do I take up" fills one in
    // with the other's answer, which is exactly what ADR 0002 decoupled.
    expect(screen.getByText("What grid does it offer the things inside it?")).toBeTruthy();
    expect(screen.getByText("What space does it take up in whatever it sits in?")).toBeTruthy();
    expect(screen.getByLabelText("Rows it offers")).toBeTruthy();
    expect(screen.getByLabelText("Rows it takes up")).toBeTruthy();
  });

  it("sends the footprint and the offered grid as the separate columns they are", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));

    // A Gridfinity-style answer: offers 3 columns of its own, takes up 2 of its
    // parent's. Nothing may cross-contaminate.
    fireEvent.change(screen.getByLabelText("Rows it offers"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("Columns it offers"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Columns it takes up"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Rows it takes up"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH");
    expect(patch?.body["grid_cols"]).toBe(3);
    expect(patch?.body["grid_rows"]).toBe(1);
    expect(patch?.body["footprint_cols"]).toBe(2);
    expect(patch?.body["footprint_rows"]).toBe(1);
  });

  it("refuses to save a grid dimension of zero, before the server has to", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));

    fireEvent.change(screen.getByLabelText("Rows it offers"), { target: { value: "0" } });
    expect(screen.getByRole("button", { name: "Save changes" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/must be a whole number of 1 or more/)).toBeTruthy();
    expect(calls.some((call) => call.method === "PATCH")).toBe(false);
  });

  it("says a seed will be copied before the button is pressed, and follows the copy", async () => {
    // The guarded outcome of this route: the row that comes back is not the row
    // in the URL. A screen that ignored that would show the untouched seed while
    // a second save minted a second copy.
    stubApi({ isSeed: true, patchClonesTo: 99 });
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Edit details" }));

    expect(screen.getByText("Saving will make you a copy")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save as my own copy" }));

    await waitFor(() => expect(calls.some((call) => call.url === "/api/container-types/99")).toBe(true));
  });
});

/** A type holds nothing. The actions that change that are on this screen. */
describe("using the type", () => {
  it("links to stamping real containers out of it", async () => {
    stubApi();
    renderScreen();
    const link = await screen.findByRole("link", { name: "Create containers from it" });
    expect(link.getAttribute("href")).toBe("/containers/new?type=7");
  });

  it("clones on request, without an edit attached, and follows the copy", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "Clone it" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/container-types/7/clone")).toBe(true),
    );
    const clone = calls.find((call) => call.url === "/api/container-types/7/clone");
    // Idempotency-guarded: a retried clone replays instead of minting a third.
    expect(typeof clone?.body["client_op_id"]).toBe("string");
    await waitFor(() => expect(calls.some((call) => call.url === "/api/container-types/99")).toBe(true));
  });
});
