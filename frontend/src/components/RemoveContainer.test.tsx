/**
 * Removing a container: the confirm flow, against a stubbed `fetch`.
 *
 * Iliana: *"I also noticed that I wasn't able to remove items in the workshop."*
 *
 * Four behaviours, and each is a way this could be actively harmful rather than
 * merely broken:
 *
 * 1. **The refusal names the contents.** A drawer with resistors in it says which
 *    resistors and how many. "Constraint failed" — or even "this still holds
 *    stock" on its own — leaves the user with nothing to do next.
 * 2. **"Cannot be undone" appears only when it is true.** The backend deletes a
 *    row nothing names and *retires* one the ledger names, so the panel asks the
 *    server first and repeats its answer. A generic warning would be a lie in one
 *    direction or the other, and the direction that matters is telling someone
 *    their drawer is gone forever when it is recoverable.
 * 3. **A subtree is an explicit second consent**, never a silent recursion.
 * 4. **Nothing is written until the button is pressed.** Opening the panel is a
 *    preview: one GET, no DELETE.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { RemoveContainer } from "./RemoveContainer";
import type { LocationRead } from "../lib/api/client";

const LOCATION = {
  id: 11,
  parent_id: 2,
  name: "Drawer 07",
  slot_label: "07",
  short_id: null,
  display: null,
  label_path: "Workshop / Workbench cabinet / 07",
  description: null,
  depth: 2,
  id_path: "/1/2/11/",
  child_count: 0,
  is_staging: false,
  is_overfull: false,
  is_placeable: true,
  esd_safe: null,
  effective_esd_safe: null,
  child_view: null,
  effective_child_view: "list",
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
    unit: "slots",
    fill_ratio: null,
    is_full: false,
    is_overfull: false,
  },
} as unknown as LocationRead;

interface Call {
  readonly url: string;
  readonly method: string;
}

const calls: Call[] = [];

/** One preview per `recursive` value, plus whatever the DELETE should answer. */
function stubApi(options: {
  preview: Record<string, unknown>;
  recursivePreview?: Record<string, unknown>;
  removed?: Record<string, unknown>;
}): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      const url = request.url;
      calls.push({ url, method: request.method });
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });

      if (request.method === "GET" && url.includes("/removal")) {
        const wantsChildren = url.includes("recursive=true");
        return json(
          wantsChildren && options.recursivePreview !== undefined
            ? options.recursivePreview
            : options.preview,
        );
      }
      if (request.method === "DELETE") {
        return json(
          options.removed ?? {
            location_id: 11,
            deleted_location_ids: [11],
            retired_location_ids: [],
            nodes: [],
          },
        );
      }
      throw new Error(`unexpected ${request.method} ${url}`);
    }),
  );
}

function mount(location: LocationRead = LOCATION) {
  return render(
    <MemoryRouter>
      <RemoveContainer location={location} onRemoved={() => undefined} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("names what is inside when it refuses", async () => {
  // The one that matters most: a refusal without the contents is not actionable.
  stubApi({
    preview: {
      location_id: 11,
      removable: false,
      reason: "holds_stock",
      message: "Drawer 07 still holds 4200 x RC0603FR-071KL (lot 3)",
      blockers: [
        {
          reason: "holds_stock",
          location_id: 11,
          label: "Drawer 07",
          label_path: "Workshop / Workbench cabinet / 07",
          detail: "4200 x RC0603FR-071KL (lot 3)",
        },
      ],
      nodes: [],
      descendant_count: 0,
    },
  });
  mount();

  fireEvent.click(screen.getByRole("button", { name: /remove this container/i }));
  await screen.findByRole("dialog");

  // `getAllByText`: the part appears in both the server's sentence and the
  // per-location list, and both saying it is the point rather than a duplicate.
  expect(screen.getAllByText(/RC0603FR-071KL/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/4200/).length).toBeGreaterThan(0);
  // And there is no way to press on regardless.
  expect(screen.queryByRole("button", { name: /^remove it$|^delete it$/i })).toBeNull();
  expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(0);
});

it("says a delete cannot be undone, and only then", async () => {
  stubApi({
    preview: {
      location_id: 11,
      removable: true,
      reason: null,
      message: null,
      blockers: [],
      nodes: [
        {
          location_id: 11,
          label: "Drawer 07",
          label_path: "Workshop / Workbench cabinet / 07",
          action: "delete",
          pins: [],
        },
      ],
      descendant_count: 0,
    },
  });
  mount();

  fireEvent.click(screen.getByRole("button", { name: /remove this container/i }));
  await screen.findByRole("dialog");
  expect(screen.getByText(/cannot be undone/i)).toBeTruthy();
  expect(screen.getByRole("button", { name: /delete it/i })).toBeTruthy();
});

it("says a retirement can be restored, and why the row stays", async () => {
  stubApi({
    preview: {
      location_id: 11,
      removable: true,
      reason: null,
      message: null,
      blockers: [],
      nodes: [
        {
          location_id: 11,
          label: "Drawer 07",
          label_path: "Workshop / Workbench cabinet / 07",
          action: "retire",
          pins: ["in_ledger"],
        },
      ],
      descendant_count: 0,
    },
    removed: {
      location_id: 11,
      deleted_location_ids: [],
      retired_location_ids: [11],
      nodes: [],
    },
  });
  mount();

  fireEvent.click(screen.getByRole("button", { name: /remove this container/i }));
  await screen.findByRole("dialog");
  expect(screen.getByText(/can be restored/i)).toBeTruthy();
  expect(screen.queryByText(/cannot be undone/i)).toBeNull();
  // The reason the row survives is stated, not hidden behind a code.
  expect(screen.getByText(/stock ledger records movements through it/i)).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: /^remove it$/i }));
  await waitFor(() => {
    expect(screen.getByText(/it can be restored/i)).toBeTruthy();
  });
});

it("needs a second, explicit consent to take the drawers with it", async () => {
  stubApi({
    preview: {
      location_id: 11,
      removable: false,
      reason: "has_children",
      message: "Cabinet still has 2 container(s) inside it (Drawer 1, Drawer 2).",
      blockers: [
        {
          reason: "has_children",
          location_id: 12,
          label: "Drawer 1",
          label_path: "Workshop / Cabinet / Drawer 1",
          detail: "would be removed too",
        },
      ],
      nodes: [],
      descendant_count: 2,
    },
    recursivePreview: {
      location_id: 11,
      removable: true,
      reason: null,
      message: null,
      blockers: [],
      nodes: [
        {
          location_id: 12,
          label: "Drawer 1",
          label_path: "Workshop / Cabinet / Drawer 1",
          action: "delete",
          pins: [],
        },
        {
          location_id: 11,
          label: "Cabinet",
          label_path: "Workshop / Cabinet",
          action: "delete",
          pins: [],
        },
      ],
      descendant_count: 2,
    },
  });
  mount();

  fireEvent.click(screen.getByRole("button", { name: /remove this container/i }));
  await screen.findByRole("dialog");
  expect(screen.getAllByText(/Drawer 1/).length).toBeGreaterThan(0);
  // Nothing to confirm yet — the recursion has not been agreed to.
  expect(screen.queryByRole("button", { name: /^delete it$/i })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: /include everything inside it/i }));
  await waitFor(() => {
    expect(screen.getByRole("button", { name: /delete it/i })).toBeTruthy();
  });
  // And the eventual DELETE carries the consent, rather than the server guessing.
  fireEvent.click(screen.getByRole("button", { name: /delete it/i }));
  await waitFor(() => {
    const removals = calls.filter((call) => call.method === "DELETE");
    expect(removals).toHaveLength(1);
    expect(removals[0]?.url).toContain("recursive=true");
  });
});

it("offers to bring back a container that was already removed", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : new Request(String(input), init);
      calls.push({ url: request.url, method: request.method });
      return new Response(
        JSON.stringify({ location_id: 11, restored_location_ids: [11], unplaced: true }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }),
  );
  const retired = { ...LOCATION, retired_at: "2026-07-29T12:00:00Z" } as LocationRead;
  const onChanged = vi.fn();
  render(
    <MemoryRouter>
      <RemoveContainer location={retired} onChanged={onChanged} />
    </MemoryRouter>,
  );

  expect(screen.getByText(/history is never deleted here/i)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /bring it back/i }));
  await waitFor(() => {
    expect(onChanged).toHaveBeenCalled();
  });
  expect(calls.some((call) => call.method === "POST" && call.url.includes("/restore"))).toBe(true);
});

it("asks through the one dialog primitive, with the caret inside it", async () => {
  // The confirmation used to be a plain in-flow `<div role="dialog">`: announced as
  // a dialog, but with no focus move, no trap, no Escape and no focus restore. And
  // it *replaced* its own trigger, so pressing "Remove this container" dropped the
  // caret to `<body>` — leaving a keyboard or screen-reader user with the most
  // consequential question in the app announced to nobody and reachable only by
  // Tab-ing from the top of the page.
  stubApi({
    preview: {
      location_id: 11,
      removable: true,
      message: "Drawer B3 will be deleted.",
      blockers: [],
      nodes: [{ location_id: 11, label: "B3", label_path: "Cabinet A / B3", action: "delete", pins: [] }],
    },
  });
  mount();

  fireEvent.click(await screen.findByRole("button", { name: "Remove this container" }));

  const dialog = await screen.findByRole("dialog", { name: /Remove Drawer 07/ });
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(dialog.contains(document.activeElement)).toBe(true);

  // And Escape is a cancel, which a hand-rolled panel had no way of hearing.
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByRole("button", { name: "Remove this container" })).toBeTruthy();
});
