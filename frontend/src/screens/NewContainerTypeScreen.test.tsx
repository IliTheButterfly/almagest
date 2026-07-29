/**
 * Authoring a container type from the app, against a stubbed `fetch`.
 *
 * What is under test is the thing two rounds of feedback said was missing: that a
 * type can be created at all, that ADR 0002's two questions are asked as two
 * questions rather than as six numeric boxes, and that the two refusals this
 * route really produces are said usefully — a 409 `duplicate_slug`, and the
 * client-side bound on a grid span that would otherwise arrive as a Pydantic 422
 * about a field name.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewContainerTypeScreen } from "./NewContainerTypeScreen";

interface Call {
  readonly url: string;
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

function createdType(body: Record<string, unknown>): Record<string, unknown> {
  return {
    id: 42,
    slug: body["slug"],
    display_name: body["display_name"],
    description: body["description"] ?? null,
    child_layout: body["child_layout"] ?? "grid",
    child_view: body["child_view"] ?? null,
    effective_child_view: "cabinet_face",
    glyph: body["glyph"] ?? null,
    photo: null,
    grid_rows: body["grid_rows"] ?? null,
    grid_cols: body["grid_cols"] ?? null,
    grid_pitch_mm: body["grid_pitch_mm"] ?? null,
    grid_height_unit_mm: body["grid_height_unit_mm"] ?? null,
    footprint_cols: body["footprint_cols"] ?? null,
    footprint_rows: body["footprint_rows"] ?? null,
    footprint_height_u: body["footprint_height_u"] ?? null,
    slot_label_scheme: body["slot_label_scheme"] ?? "row_alpha_col_num",
    slot_label_params: body["slot_label_params"] ?? null,
    materialize_slots: false,
    capacity_model: body["capacity_model"] ?? "slots",
    capacity_slots: body["capacity_slots"] ?? null,
    max_parts_per_slot: null,
    inner_length_mm: body["inner_length_mm"] ?? null,
    inner_width_mm: body["inner_width_mm"] ?? null,
    inner_height_mm: body["inner_height_mm"] ?? null,
    default_fill_factor: 0.85,
    full_threshold: 0.9,
    esd_safe: null,
    is_placeable: body["is_placeable"] ?? true,
    max_item_dimension_mm: null,
    allowed_part_kinds: null,
    front_width_mm: null,
    front_height_mm: null,
    is_seed: false,
  };
}

function stubApi(options: { conflict?: boolean } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ url: url.pathname, method: request.method, body });

      if (url.pathname === "/api/container-types" && request.method === "POST") {
        if (options.conflict === true) {
          return json(
            {
              detail: {
                reason: "duplicate_slug",
                message: "a container type with slug 'raaco-c8-30' already exists",
              },
            },
            409,
          );
        }
        return json({ container_type: createdType(body), replayed: false }, 201);
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen() {
  return render(
    <MemoryRouter initialEntries={["/container-types/new"]}>
      <Routes>
        <Route path="/container-types/new" element={<NewContainerTypeScreen />} />
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

describe("authoring a container type", () => {
  it("points at cloning a seed first, since a blank form is the slower route", () => {
    stubApi();
    renderScreen();
    expect(screen.getByText("Cloning an existing one is usually faster")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Browse the library and clone one/ })).toBeTruthy();
  });

  it("asks ADR 0002's two questions separately, in those terms", () => {
    stubApi();
    renderScreen();
    expect(screen.getByText("What grid does it offer the things inside it?")).toBeTruthy();
    expect(screen.getByText("What space does it take up in whatever it sits in?")).toBeTruthy();
    // The dimension labels say which question they belong to, so "rows" is never
    // ambiguous between the two.
    expect(screen.getByLabelText("Rows it offers")).toBeTruthy();
    expect(screen.getByLabelText("Rows it takes up")).toBeTruthy();
  });

  it("suggests a slug from the name and stops once the slug is typed in", () => {
    stubApi();
    renderScreen();
    const name = screen.getByLabelText("Name");
    const slug = screen.getByLabelText(/^Slug/);

    fireEvent.change(name, { target: { value: "Raaco C8-30" } });
    expect((slug as HTMLInputElement).value).toBe("raaco-c8-30");

    fireEvent.change(slug, { target: { value: "my-raaco" } });
    fireEvent.change(name, { target: { value: "Raaco C8-30 mine" } });
    // A deliberate choice is never overwritten by a suggestion.
    expect((slug as HTMLInputElement).value).toBe("my-raaco");
  });

  it("posts both answers as the separate columns they are", async () => {
    stubApi();
    renderScreen();

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Gridfinity plate 6x4" } });
    fireEvent.change(screen.getByLabelText("Rows it offers"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("Columns it offers"), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText("Grid pitch (mm)"), { target: { value: "42" } });
    fireEvent.change(screen.getByLabelText("Columns it takes up"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create this type" }));

    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    const post = calls.find((call) => call.method === "POST");
    expect(post?.body["slug"]).toBe("gridfinity-plate-6x4");
    expect(post?.body["grid_rows"]).toBe(4);
    expect(post?.body["grid_cols"]).toBe(6);
    expect(post?.body["grid_pitch_mm"]).toBe(42);
    expect(post?.body["footprint_cols"]).toBe(1);
    // Untouched nullable columns go as null, not as zero.
    expect(post?.body["footprint_rows"]).toBeNull();
    expect(post?.body["capacity_slots"]).toBeNull();
    // Idempotency-guarded: a doubled tap must not file two types.
    expect(typeof post?.body["client_op_id"]).toBe("string");
  });

  it("offers the label zero-padding only for the scheme that has numbers to pad", async () => {
    stubApi();
    renderScreen();
    expect(screen.queryByLabelText(/Pad the numbers/)).toBeNull();

    fireEvent.change(screen.getByLabelText("How its slots are labelled"), {
      target: { value: "sequential" },
    });
    fireEvent.change(screen.getByLabelText(/Pad the numbers/), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Raaco" } });
    fireEvent.click(screen.getByRole("button", { name: "Create this type" }));

    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(calls.find((call) => call.method === "POST")?.body["slot_label_params"]).toEqual({
      zero_pad: 2,
    });
  });

  it("says what is left to do rather than dropping you on an editor", async () => {
    stubApi();
    renderScreen();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Bench drawer unit" } });
    fireEvent.click(screen.getByRole("button", { name: "Create this type" }));

    // A type holds nothing until containers are stamped from it, so both next
    // steps are named and linked.
    expect(await screen.findByText("Created. Two things left to do.")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Lay out its slots/ }).getAttribute("href")).toBe(
      "/container-types/42",
    );
    expect(
      screen.getByRole("link", { name: /Create real containers from it/ }).getAttribute("href"),
    ).toBe("/containers/new?type=42");
  });

  it("explains a 409 duplicate slug as a permanent field, not as a generic failure", async () => {
    stubApi({ conflict: true });
    renderScreen();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Raaco C8-30" } });
    fireEvent.click(screen.getByRole("button", { name: "Create this type" }));

    expect(await screen.findByText(/Another container type already uses that slug/)).toBeTruthy();
    // The server's own message is kept underneath rather than replaced.
    expect(screen.getByText(/already exists/)).toBeTruthy();
    expect(screen.getByText(/409/)).toBeTruthy();
  });

  it("will not post a grid span of zero, which the API bounds at one", () => {
    stubApi();
    renderScreen();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Broken" } });
    fireEvent.change(screen.getByLabelText("Columns it offers"), { target: { value: "0" } });

    expect(screen.getByRole("button", { name: "Create this type" }).hasAttribute("disabled")).toBe(
      true,
    );
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });
});
