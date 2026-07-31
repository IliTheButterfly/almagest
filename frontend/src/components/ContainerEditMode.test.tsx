/**
 * Edit mode: the same container page, turned into its own editor.
 *
 * Iliana: *"I don't like the multiple pages per container to edit them. I'd much
 * prefer a page and an edit mode... you can customize the UI (storage) or use it
 * normally."* So these mount the real `LocationScreen` rather than the component in
 * isolation — the thing being asserted is that a user standing on a container's page
 * can reach every edit from it, and a test of the panel alone cannot say that.
 *
 * The five behaviours, and why each is here rather than assumed:
 *
 * 1. **The toggle switches modes and says which one it is in.** Using storage and
 *    rearranging the furniture are different intentions, and a mis-tap between them
 *    is expensive, so the mode is marked by a word and a band, not a tint.
 * 2. **A rename happens where the container stands** — a panel over the page, and
 *    the page's heading afterwards agrees with the server.
 * 3. **An unsent edit is visibly unsent, and is not walked away from silently.**
 *    A panel is one Escape from gone; the badge and the discard prompt are what
 *    stand between a typed name and losing it.
 * 4. **Adding and removing containers are both on this page.** No "go to the layout
 *    editor" step, and the remove affordance from the removal work is hosted here.
 * 5. **Every level gets the identical editor.** The last test renders depth 0, 2 and
 *    4 and asserts the *same* affordances come out of the *same* component. That is
 *    the recursion claim `lib/locations/views.ts` documents, and asserting it is the
 *    only way to keep it: a branch on "is this a room" would pass every other test
 *    in this file.
 */

import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { LocationScreen } from "../screens/LocationScreen";

/** A full `LocationRead`, at whatever depth the caller wants. */
function locationRead(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 11,
    parent_id: 2,
    name: "Workbench cabinet",
    slot_label: null,
    short_id: "4K7T92M8",
    display: null,
    label_path: "Workshop / Workbench cabinet",
    description: null,
    depth: 1,
    id_path: "/2/11/",
    child_count: 0,
    lot_count: 0,
    qty_milli: 0,
    is_staging: false,
    is_overfull: false,
    is_placeable: true,
    esd_safe: null,
    effective_esd_safe: null,
    child_view: null,
    effective_child_view: "cabinet_face",
    glyph: null,
    effective_glyph: null,
    photo: null,
    effective_photo: null,
    container_type_id: 7,
    placement: null,
    access_score: 0.5,
    tare_mg: null,
    last_printed_at: null,
    retired_at: null,
    lots: [],
    capacity: {
      model: "slots",
      used: 0,
      capacity: 30,
      unit: "slots",
      fill_ratio: 0,
      is_full: false,
      is_overfull: false,
    },
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

/**
 * One stub for the whole page, holding the container's *current* state — so a
 * rename can be asserted the way the user sees it: the page re-reads, and the
 * heading changes.
 */
function stubApi(initial: Record<string, unknown> = locationRead()): { current: () => Record<string, unknown> } {
  let current = initial;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = new URL(request.url);
      const raw = request.method === "GET" || request.method === "DELETE" ? "" : await request.text();
      const body = raw === "" ? {} : (JSON.parse(raw) as Record<string, unknown>);
      calls.push({ path: url.pathname, method: request.method, body });
      const id = current["id"] as number;

      if (url.pathname === `/api/locations/${id}` && request.method === "GET") {
        return json(current);
      }
      if (url.pathname === `/api/locations/${id}/details` && request.method === "PUT") {
        current = { ...current, name: body["name"], description: body["description"] };
        return json({
          location_id: id,
          name: body["name"],
          description: body["description"],
          esd_safe: body["esd_safe"],
          is_placeable: body["is_placeable"],
          effective_esd_safe: null,
          replayed: false,
        });
      }
      if (url.pathname === "/api/container-types" && request.method === "GET") {
        return json([
          {
            id: 7,
            display_name: "Raaco 15-drawer",
            is_seed: false,
            glyph: null,
            capacity_model: "slots",
            child_view: null,
            effective_child_view: "cabinet_face",
            presents_grid: true,
            occupies_slot: false,
            grid_rows: 3,
            grid_cols: 5,
          },
        ]);
      }
      if (url.pathname === "/api/locations" && request.method === "POST") {
        return json({
          location: locationRead({ id: 99, name: body["name"], parent_id: body["parent_id"] }),
          created: true,
          replayed: false,
        });
      }
      if (url.pathname === `/api/locations/${id}/removal` && request.method === "GET") {
        return json({
          location_id: id,
          removable: true,
          reason: null,
          message: null,
          blockers: [],
          nodes: [
            {
              location_id: id,
              label: current["name"],
              label_path: current["label_path"],
              action: "delete",
              pins: [],
            },
          ],
          descendant_count: 0,
        });
      }
      if (url.pathname === `/api/locations/${id}` && request.method === "DELETE") {
        return json({
          location_id: id,
          deleted_location_ids: [id],
          retired_location_ids: [],
          nodes: [],
        });
      }
      throw new Error(`unstubbed request: ${request.method} ${url.pathname}`);
    }),
  );
  return { current: () => current };
}

/** The URL, so "which panel is open" can be read the way a deep link writes it. */
function Url() {
  const location = useLocation();
  return <p data-testid="url">{`${location.pathname}${location.search}`}</p>;
}

function renderPage(entry = "/locations/11"): void {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Url />
      <Routes>
        <Route path="/locations/:locationId" element={<LocationScreen />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** The edit-mode card, found by the word that marks the mode. */
function editCard(): HTMLElement {
  const card = screen.getByText("edit mode").closest(".card");
  if (card === null) {
    throw new Error("the edit-mode card is not on the page");
  }
  return card as HTMLElement;
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("turns the page into its own editor, and back, without navigating away", async () => {
  stubApi();
  renderPage();

  const toggle = await screen.findByRole("button", { name: "Edit this container" });
  expect(toggle.getAttribute("aria-pressed")).toBe("false");
  expect(screen.queryByText("edit mode")).toBeNull();

  fireEvent.click(toggle);

  // Same page — the path never changes, because it is a `/s/{short_id}` redirect
  // target; only the query says the mode.
  expect(screen.getByTestId("url").textContent).toBe("/locations/11?edit=1");
  expect(screen.getByText("edit mode")).toBeTruthy();
  const done = screen.getByRole("button", { name: "Done editing" });
  expect(done.getAttribute("aria-pressed")).toBe("true");

  fireEvent.click(done);
  expect(screen.queryByText("edit mode")).toBeNull();
  expect(screen.getByTestId("url").textContent).toBe("/locations/11");
});

it("opens the panel a deep link names, so the old editing URLs still land somewhere", async () => {
  // `/locations/:id/layout` redirects here with `?edit=1&panel=layout`; the same
  // mechanism is what makes a panel shareable at all.
  stubApi();
  renderPage("/locations/11?edit=1&panel=details");

  expect(await screen.findByRole("dialog")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Name and description" })).toBeTruthy();
});

it("renames a container where it stands", async () => {
  const api = stubApi();
  renderPage("/locations/11?edit=1");

  fireEvent.click(await screen.findByRole("button", { name: "Name and description…" }));
  const dialog = await screen.findByRole("dialog");
  // The caret is in the field, not on the close button.
  const name = within(dialog).getByLabelText("Name");
  expect(document.activeElement).toBe(name);

  fireEvent.change(name, { target: { value: "Bench cabinet" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

  await waitFor(() =>
    expect(calls.some((call) => call.method === "PUT" && call.path.endsWith("/details"))).toBe(true),
  );
  const put = calls.find((call) => call.method === "PUT" && call.path.endsWith("/details"));
  expect(put?.body["name"]).toBe("Bench cabinet");
  expect(api.current()["name"]).toBe("Bench cabinet");

  // The panel closes and the page agrees with the server.
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(await screen.findByRole("heading", { level: 1, name: "Bench cabinet" })).toBeTruthy();
});

it("says an unsent rename is unsent, and does not throw it away on a stray Escape", async () => {
  stubApi();
  renderPage("/locations/11?edit=1");

  fireEvent.click(await screen.findByRole("button", { name: "Name and description…" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).queryByText("unsaved")).toBeNull();
  expect(within(dialog).getByText("Saved.")).toBeTruthy();

  fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Half a name" } });
  // Two statements of the same fact, one at the close button and one at Save.
  expect(within(dialog).getByText("unsaved").className).toContain("badge-warn");
  expect(within(dialog).getByText("Not saved yet.")).toBeTruthy();

  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.getByRole("dialog")).toBeTruthy();
  expect(screen.getByText(/Nothing here has been saved yet/)).toBeTruthy();
  expect(calls.some((call) => call.method === "PUT")).toBe(false);

  fireEvent.click(screen.getByRole("button", { name: "Keep editing" }));
  expect((within(screen.getByRole("dialog")).getByLabelText("Name") as HTMLInputElement).value).toBe(
    "Half a name",
  );

  // And the deliberate second answer does close it, with nothing written.
  fireEvent.keyDown(document, { key: "Escape" });
  fireEvent.click(screen.getByRole("button", { name: "Discard the changes" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(calls.some((call) => call.method === "PUT")).toBe(false);
});

it("adds a container inside this one from this page, carrying it as the parent", async () => {
  stubApi();
  renderPage("/locations/11?edit=1");

  fireEvent.click(await screen.findByRole("button", { name: "Add containers inside…" }));
  const dialog = await screen.findByRole("dialog");
  fireEvent.click(within(dialog).getByRole("button", { name: "One plain container" }));
  fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "Drawer 07" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Create it" }));

  await waitFor(() =>
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/locations")).toBe(
      true,
    ),
  );
  const post = calls.find((call) => call.method === "POST" && call.path === "/api/locations");
  expect(post?.body["parent_id"]).toBe(11);
  expect(post?.body["name"]).toBe("Drawer 07");
  expect(await screen.findByText(/1 container\(s\) created/)).toBeTruthy();
});

it("hosts removing the container, with the consequence stated before the confirm", async () => {
  stubApi();
  renderPage("/locations/11?edit=1");

  fireEvent.click(await screen.findByRole("button", { name: "Remove this container" }));
  // The preview writes nothing, and says which of delete-or-retire this is.
  await waitFor(() => expect(screen.getByText(/cannot be undone/i)).toBeTruthy());
  expect(calls.some((call) => call.method === "DELETE")).toBe(false);

  fireEvent.click(screen.getByRole("button", { name: /delete it/i }));
  await waitFor(() =>
    expect(calls.some((call) => call.method === "DELETE" && call.path === "/api/locations/11")).toBe(
      true,
    ),
  );
});

/**
 * The recursion claim, asserted rather than claimed.
 *
 * A room at the top of the tree, a drawer bank two levels down and a divider four
 * levels down are edited by the identical component with the identical affordances.
 * Nothing in edit mode may ask "am I at the top?" or "is this a cabinet?" — the
 * schema has no named levels, so a renderer that invents them grows one special
 * case per level and disagrees with the tree it is drawing.
 */
it("offers the identical editor at every depth", async () => {
  const affordances: string[][] = [];
  for (const fixture of [
    // The outermost level: a room, whose parent is the world.
    locationRead({ id: 11, parent_id: null, depth: 0, name: "Workshop", label_path: "Workshop" }),
    locationRead({ id: 11, depth: 2, name: "Drawer bank", label_path: "Workshop / A / Drawer bank" }),
    // Deep: a divider inside a bin inside a drawer.
    locationRead({
      id: 11,
      depth: 4,
      name: "Divider 3",
      slot_label: "3",
      label_path: "Workshop / A / Drawer bank / Bin 2 / Divider 3",
    }),
  ]) {
    stubApi(fixture);
    const view = render(
      <MemoryRouter initialEntries={["/locations/11?edit=1"]}>
        <Routes>
          <Route path="/locations/:locationId" element={<LocationScreen />} />
        </Routes>
      </MemoryRouter>,
    );
    await screen.findByText("edit mode");
    affordances.push(
      within(editCard())
        .getAllByRole("button")
        .map((button) => button.textContent ?? ""),
    );
    view.unmount();
    vi.unstubAllGlobals();
  }

  const [top, middle, deep] = affordances;
  expect(top?.length).toBeGreaterThan(0);
  expect(middle).toEqual(top);
  expect(deep).toEqual(top);
  // And the five panels plus the removal are all of them, at each depth. The room
  // plan is on this list on purpose: nothing is validated against `child_view`
  // (ADR 0006), so a container drawn as a cabinet face can still be given a plan,
  // and gating the button on the view would be the first "is this a room?" branch.
  expect(top).toEqual([
    "Name and description…",
    "Picture…",
    "Slots inside…",
    "Room plan…",
    "Add containers inside…",
    "Remove this container",
  ]);
});
