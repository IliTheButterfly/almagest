/**
 * Turning a container type into containers you can actually put parts in.
 *
 * The behaviour under test is the shape of `POST /api/locations/{id}/instantiate`
 * and of the plain `POST /api/locations` beside it, plus the two refusals that
 * are worth their own words: a 422 `bad_naming_pattern` (only `{n}` may be
 * substituted) and a 409 `pitch_mismatch`, which — unlike everything else in the
 * capacity area — is a hard refusal and has to read like one rather than like
 * another advisory flag.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NewContainersScreen } from "./NewContainersScreen";

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

function node(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 1,
    name: "Workshop",
    parent_id: null,
    depth: 0,
    id_path: "/1/",
    label_path: "Workshop",
    container_type_id: null,
    slot_label: null,
    is_overfull: false,
    is_staging: false,
    is_placeable: true,
    effective_child_view: "floor_plan",
    effective_glyph: "room",
    fill_ratio: null,
    lot_count: 0,
    qty_milli: 0,
    ...overrides,
  };
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

function location(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 50,
    name: "Cabinet 1",
    description: null,
    parent_id: 1,
    depth: 1,
    id_path: "/1/50/",
    label_path: "Workshop / Cabinet 1",
    container_type_id: 7,
    slot_label: null,
    esd_safe: null,
    effective_esd_safe: null,
    is_placeable: true,
    child_view: null,
    effective_child_view: "cabinet_face",
    glyph: null,
    effective_glyph: "cabinet",
    photo: null,
    effective_photo: null,
    is_overfull: false,
    is_staging: false,
    access_score: 0.5,
    tare_mg: null,
    short_id: "4K7T92M8",
    display: null,
    child_count: 30,
    capacity: {
      model: "slots",
      capacity: 30,
      used: 0,
      fill_ratio: 0,
      is_full: false,
      is_overfull: false,
      unit: "slots",
    },
    lots: [],
    last_printed_at: null,
    ...overrides,
  };
}

function stubApi(
  options: {
    nodes?: Record<string, unknown>[];
    types?: Record<string, unknown>[];
    instantiate?: { status: number; body: unknown };
  } = {},
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const url = new URL(request.url);
      const raw = request.method === "GET" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ url: url.pathname, method: request.method, body });

      if (url.pathname === "/api/container-types" && request.method === "GET") {
        return json(options.types ?? [containerType()]);
      }
      if (url.pathname === "/api/locations/tree" && request.method === "GET") {
        return json({ nodes: options.nodes ?? [node()] });
      }
      if (
        (url.pathname === "/api/locations/1/instantiate" ||
          url.pathname === "/api/locations/instantiate") &&
        request.method === "POST"
      ) {
        if (options.instantiate !== undefined) {
          return json(options.instantiate.body, options.instantiate.status);
        }
        const count = Number(body["count"] ?? 1);
        return json(
          {
            locations: Array.from({ length: count }, (_, index) =>
              location({
                id: 50 + index,
                name: `Cabinet ${index + 1}`,
                label_path: `Workshop / Cabinet ${index + 1}`,
              }),
            ),
            replayed: false,
          },
          201,
        );
      }
      if (url.pathname === "/api/locations" && request.method === "POST") {
        return json(
          {
            location: location({
              id: 90,
              name: String(body["name"]),
              parent_id: body["parent_id"] ?? null,
              container_type_id: null,
              child_count: 0,
              label_path: String(body["name"]),
            }),
            replayed: false,
          },
          201,
        );
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
}

function renderScreen(search = ""): void {
  render(
    <MemoryRouter initialEntries={[`/containers/new${search}`]}>
      <Routes>
        <Route path="/containers/new" element={<NewContainersScreen />} />
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

describe("stamping containers from a type", () => {
  it("arrives with the position already chosen when the tree sent one", async () => {
    stubApi();
    renderScreen("?parent=1");
    expect(await screen.findByText(/Inside/)).toBeTruthy();
    expect(screen.getByText("Workshop", { selector: ".mono" })).toBeTruthy();
  });

  it("posts the count, the pattern and the tag granularity to the parent's route", async () => {
    stubApi();
    renderScreen("?parent=1&type=7");
    await screen.findByRole("button", { name: /Create/ });

    fireEvent.change(screen.getByLabelText("How many"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Name them"), { target: { value: "Drawer unit {n}" } });
    fireEvent.change(screen.getByLabelText("What gets a printed id"), { target: { value: "slot" } });
    fireEvent.click(screen.getByRole("button", { name: "Create 3 container(s)" }));

    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    const post = calls.find((call) => call.method === "POST");
    expect(post?.url).toBe("/api/locations/1/instantiate");
    expect(post?.body).toMatchObject({
      container_type_id: 7,
      count: 3,
      naming_pattern: "Drawer unit {n}",
      tag_granularity: "slot",
    });
    // Writes a whole subtree per instance, so a retry must replay rather than
    // stamp the cabinet twice.
    expect(typeof post?.body["client_op_id"]).toBe("string");
  });

  it("shows what the containers will be called before creating them", async () => {
    stubApi();
    renderScreen("?parent=1&type=7");
    await screen.findByLabelText("Name them");

    fireEvent.change(screen.getByLabelText("How many"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("Name them"), { target: { value: "Drawer {n}" } });
    expect(screen.getByText("Drawer 1, Drawer 2, … Drawer 4")).toBeTruthy();
  });

  it("warns about a pattern the server will refuse, and does not send it", async () => {
    stubApi();
    renderScreen("?parent=1&type=7");
    await screen.findByLabelText("Name them");

    fireEvent.change(screen.getByLabelText("Name them"), { target: { value: "Drawer {oops}" } });
    expect(screen.getByText("That name pattern will be refused")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Create/ }).hasAttribute("disabled")).toBe(true);
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("explains the server's own bad_naming_pattern rather than a generic failure", async () => {
    // The client preview only catches braces; a pattern that slips through still
    // has to read as itself. `{{n}}`-style escapes are the server's business.
    stubApi({
      instantiate: {
        status: 422,
        body: {
          detail: {
            reason: "bad_naming_pattern",
            message: "naming_pattern 'Drawer' is not a valid template; only {n} may be substituted",
          },
        },
      },
    });
    renderScreen("?parent=1&type=7");
    await screen.findByLabelText("Name them");

    fireEvent.change(screen.getByLabelText("Name them"), { target: { value: "Drawer" } });
    fireEvent.click(screen.getByRole("button", { name: /Create/ }));

    expect(await screen.findByText(/Only \{n\} can be filled in in a name pattern/)).toBeTruthy();
    expect(screen.getByText(/bad_naming_pattern/)).toBeTruthy();
  });

  it("says a pitch mismatch is refused rather than flagged", async () => {
    // The one hard geometric refusal in a system where capacity is advisory
    // everywhere else — the wording has to carry that difference.
    stubApi({
      instantiate: {
        status: 409,
        body: {
          detail: {
            reason: "pitch_mismatch",
            message: "'gridfinity-bin-2x1x6' cannot sit in 'Workshop''s grid: pitch_mismatch",
          },
        },
      },
    });
    renderScreen("?parent=1&type=7");
    await screen.findByLabelText("Name them");
    fireEvent.click(screen.getByRole("button", { name: /Create/ }));

    expect(await screen.findByText(/physically will not seat/)).toBeTruthy();
    expect(screen.getByText(/refused rather than flagged/)).toBeTruthy();
  });

  it("lists what it created, with links to each one", async () => {
    stubApi();
    renderScreen("?parent=1&type=7");
    await screen.findByLabelText("How many");

    fireEvent.change(screen.getByLabelText("How many"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Create 2 container(s)" }));

    expect(await screen.findByText("2 container(s) created")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Cabinet 1/ }).getAttribute("href")).toBe("/locations/50");
    // Each instance owns its copy of the layout from this moment on, which is
    // what makes editing the type afterwards safe.
    expect(screen.getByText(/own copy of the layout/)).toBeTruthy();
  });

  /**
   * The empty-install case, which used to be a dead end: with no parent chosen
   * the form refused to submit and told you to go and create a plain container
   * instead, so the first container in a fresh database could never be a typed
   * one — a room to draw or a cabinet with drawers had nowhere to go.
   */
  it("stamps at the top of the tree when nothing holds them", async () => {
    stubApi({ nodes: [] });
    renderScreen("?type=7");
    await screen.findByLabelText("Which type");

    expect(screen.getByText("These go at the top of the tree")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Create 1 container(s)" }));

    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    const post = calls.find((call) => call.method === "POST");
    // The parentless twin of the route above — a URL cannot carry an absent id.
    expect(post?.url).toBe("/api/locations/instantiate");
    expect(post?.body).toMatchObject({ container_type_id: 7, count: 1 });
  });
});

describe("a plain container", () => {
  it("creates a top-level one with no parent at all", async () => {
    stubApi({ nodes: [] });
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "One plain container" }));

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Workshop" } });
    fireEvent.click(screen.getByRole("button", { name: "Create it" }));

    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    const post = calls.find((call) => call.method === "POST");
    expect(post?.url).toBe("/api/locations");
    expect(post?.body).toMatchObject({ name: "Workshop", parent_id: null, is_placeable: true });
  });

  it("can be marked as holding only other containers", async () => {
    stubApi();
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "One plain container" }));

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Rack" } });
    fireEvent.click(screen.getByLabelText("Stock can be put directly into it"));
    fireEvent.click(screen.getByRole("button", { name: "Create it" }));

    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(calls.find((call) => call.method === "POST")?.body["is_placeable"]).toBe(false);
  });

  it("starts on the plain form when there are no types to stamp from", async () => {
    stubApi({ types: [], nodes: [] });
    renderScreen();
    // Not just reachable — already open, because with no types the other mode
    // cannot do anything at all.
    expect(await screen.findByRole("heading", { name: "One plain container" })).toBeTruthy();
    expect(screen.getByLabelText("Name")).toBeTruthy();
  });

  it("disables 'Create it' and says why while the name is blank, rather than doing nothing", async () => {
    stubApi({ nodes: [] });
    renderScreen();
    fireEvent.click(await screen.findByRole("button", { name: "One plain container" }));

    // Nothing typed yet — the empty starting state itself must not be silent.
    expect(screen.getByRole("button", { name: "Create it" })).toHaveProperty("disabled", true);
    expect(screen.getByText(/Give it a name/)).toBeTruthy();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });
});
