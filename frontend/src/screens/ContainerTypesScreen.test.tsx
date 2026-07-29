/**
 * The container-type library.
 *
 * The point of this screen is discoverability, so that is what is asserted: the
 * shipped types are visible, cloning one is a button on its own row rather than
 * something to find, and each row states ADR 0002's two answers separately —
 * "offers 30 x 1" and "takes up nothing measured" are the two facts somebody is
 * choosing between when they pick which type to copy.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ContainerTypesScreen } from "./ContainerTypesScreen";

interface Call {
  readonly url: string;
  readonly search: string;
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

function containerType(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 7,
    slug: "raaco-c8-30",
    display_name: "Raaco C8-30",
    description: null,
    child_layout: "list",
    child_view: null,
    effective_child_view: "cabinet_face",
    glyph: "cabinet",
    photo: null,
    grid_rows: 30,
    grid_cols: 1,
    grid_pitch_mm: null,
    grid_height_unit_mm: null,
    footprint_cols: null,
    footprint_rows: null,
    footprint_height_u: null,
    slot_label_scheme: "sequential",
    slot_label_params: { zero_pad: 2 },
    materialize_slots: false,
    capacity_model: "slots",
    capacity_slots: 30,
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
    is_seed: true,
    ...overrides,
  };
}

const BIN = containerType({
  id: 9,
  slug: "gridfinity-bin-2x1x6",
  display_name: "Gridfinity bin 2x1x6u",
  glyph: "bin",
  grid_rows: null,
  grid_cols: null,
  grid_pitch_mm: 42,
  footprint_cols: 2,
  footprint_rows: 1,
  footprint_height_u: 6,
  capacity_model: "volume",
  capacity_slots: null,
});

function stubApi(options: { cloneStatus?: number } = {}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ url: url.pathname, search: url.search, method: request.method, body });

      if (url.pathname === "/api/container-types" && request.method === "GET") {
        const seedOnly = url.searchParams.get("is_seed");
        const all = [
          containerType(),
          BIN,
          containerType({
            id: 11,
            slug: "mine",
            display_name: "Mine",
            is_seed: false,
            child_layout: "none",
            grid_rows: null,
            grid_cols: null,
            capacity_model: "none",
            capacity_slots: null,
          }),
        ];
        const filtered =
          seedOnly === null ? all : all.filter((type) => type["is_seed"] === (seedOnly === "true"));
        return json(filtered);
      }
      if (url.pathname === "/api/container-types/7/clone" && request.method === "POST") {
        if (options.cloneStatus === 409) {
          return json(
            {
              detail: {
                reason: "duplicate_slug",
                message: "a container type with slug 'raaco-c8-30-copy' already exists",
              },
            },
            409,
          );
        }
        return json(
          {
            container_type: containerType({ id: 77, slug: "raaco-c8-30-copy", is_seed: false }),
            replayed: false,
          },
          201,
        );
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}${url.search}`);
    }),
  );
}

function renderScreen(): void {
  render(
    <MemoryRouter initialEntries={["/container-types"]}>
      <Routes>
        <Route path="/container-types" element={<ContainerTypesScreen />} />
        <Route path="/container-types/:id" element={<div>the copy</div>} />
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

describe("the container-type library", () => {
  it("shows the shipped types, and says editing one copies it", async () => {
    stubApi();
    renderScreen();
    expect(await screen.findByRole("link", { name: "Raaco C8-30" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Gridfinity bin 2x1x6u" })).toBeTruthy();
    expect(screen.getAllByText("shipped — editing copies it").length).toBeGreaterThan(0);
  });

  it("states the two ADR 0002 answers as two separate lines", async () => {
    stubApi();
    renderScreen();
    await screen.findByRole("link", { name: "Raaco C8-30" });

    // A cabinet offers a grid and takes up nothing measured; a bin is the
    // opposite. Conflating them is exactly the mistake the ADR decoupled, so the
    // list does not merge them into one "size" line either.
    expect(screen.getByText(/Offers: offers 30 x 1/)).toBeTruthy();
    expect(screen.getByText("Takes up: takes up 2 x 1, 6u tall")).toBeTruthy();
    expect(screen.getByText(/Offers: no grid declared/)).toBeTruthy();
  });

  it("offers a blank form and the route to real containers from the top of the screen", async () => {
    stubApi();
    renderScreen();
    expect((await screen.findByRole("link", { name: "New type" })).getAttribute("href")).toBe(
      "/container-types/new",
    );
    expect(
      screen.getByRole("link", { name: /Add real containers to storage/ }).getAttribute("href"),
    ).toBe("/containers/new");
  });

  it("clones a type from its own row and lands on the copy", async () => {
    stubApi();
    renderScreen();
    await screen.findByRole("link", { name: "Raaco C8-30" });

    fireEvent.click(screen.getAllByRole("button", { name: "Clone" })[0] as HTMLElement);

    await waitFor(() =>
      expect(calls.some((call) => call.url === "/api/container-types/7/clone")).toBe(true),
    );
    // No slug given: the server picks `{slug}-copy`, so two clones of one seed do
    // not collide with each other.
    expect(calls.find((call) => call.method === "POST")?.body["slug"]).toBeUndefined();
    expect(await screen.findByText("the copy")).toBeTruthy();
  });

  it("explains a slug collision on clone instead of failing silently", async () => {
    stubApi({ cloneStatus: 409 });
    renderScreen();
    await screen.findByRole("link", { name: "Raaco C8-30" });

    fireEvent.click(screen.getAllByRole("button", { name: "Clone" })[0] as HTMLElement);
    expect(await screen.findByText(/Another container type already uses that slug/)).toBeTruthy();
  });

  it("filters to the ones you made yourself, through the API's own flag", async () => {
    stubApi();
    renderScreen();
    await screen.findByRole("link", { name: "Raaco C8-30" });

    fireEvent.click(screen.getByRole("button", { name: "Mine" }));

    await waitFor(() => expect(calls.some((call) => call.search === "?is_seed=false")).toBe(true));
    expect(await screen.findByRole("link", { name: "Mine" })).toBeTruthy();
  });
});
